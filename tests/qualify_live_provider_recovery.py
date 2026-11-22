"""Capture live Rover traffic and qualify a real signed provider round trip."""

import argparse
import asyncio
import hmac
import json
import math
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


FEEDS = ("frame:front", "sensor_tick:front", "frame:rear", "sensor_tick:rear")
CONSUMERS = ("public", "bridge")


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def assess(evidence, window_seconds, max_gap_seconds, max_delivery_age_seconds):
    """Report every missing or failed production assertion."""
    failures = []
    submit = evidence.get("submission") or {}
    operation, lease = submit.get("operation"), submit.get("lease")
    if submit.get("status") != 200 or not operation or not lease:
        failures.append(f"signed submission did not confirm an operation and lease (HTTP {submit.get('status')})")

    checks = evidence.get("results") or []
    observed_leases = {check["lease"] for check in checks if check.get("status") == 200
                       and check.get("operation") == operation and isinstance(check.get("lease"), str)}
    if not lease and len(observed_leases) == 1:
        lease = observed_leases.pop()
    for check in checks:
        if check.get("status") != 200:
            failures.append(f"signed result read returned HTTP {check.get('status')}")
    if not any(check.get("status") == 200 and check.get("operation") == operation
               and check.get("lease") == lease and check.get("state") == "succeeded"
               and check.get("has_result") is True for check in checks):
        failures.append("the normal signed API did not return the completed result for this operation and lease")

    release = evidence.get("lease_check") or {}
    if (not lease or release.get("operation") != operation
            or release.get("lease") != lease or release.get("state") != "released"):
        failures.append("release of the exact lease was not observed")

    streams = evidence.get("streams") or {}
    for consumer in CONSUMERS:
        stream = streams.get(consumer) or {}
        seconds = stream.get("seconds")
        if stream.get("error"):
            failures.append(f"{consumer} socket failed ({stream['error']})")
        if not positive(seconds) or seconds < window_seconds:
            failures.append(f"{consumer} socket did not complete the {window_seconds:g}s observation window")
        for feed in FEEDS:
            sample = (stream.get("feeds") or {}).get(feed) or {}
            count, gap = sample.get("records"), sample.get("max_gap_seconds")
            if type(count) is not int or count < 2:
                failures.append(f"{consumer} {feed} delivered fewer than two distinct records")
                continue
            if not positive(gap) or gap > max_gap_seconds:
                failures.append(f"{consumer} {feed} exceeded the {max_gap_seconds:g}s continuity budget ({gap}s)")
            age = sample.get("max_delivery_age_seconds")
            if (sample.get("unclocked_records") != 0 or type(age) not in (int, float)
                    or not math.isfinite(age) or not 0 <= age <= max_delivery_age_seconds):
                failures.append(f"{consumer} {feed} failed the {max_delivery_age_seconds:g}s delivery-age check ({age}s)")
    return failures


def feed_record(data, *, include_unidentified=False):
    if len(data) < 4:
        return None
    header_size = struct.unpack_from("!I", data)[0]
    end = 4 + header_size
    if not 4 < end <= len(data):
        return None
    try:
        metadata = json.loads(data[4:end])
    except (UnicodeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    kind = metadata.get("type")
    source = metadata.get("camera") or metadata.get("camera_id") or metadata.get("source")
    name = f"{kind}:{source}"
    if name not in FEEDS or (kind == "frame" and len(data) == end):
        return None
    packet = metadata.get("sensor_packet")
    if isinstance(packet, dict):
        capture = packet.get("capture_monotonic_us")
        boot, receiver = packet.get("boot_id"), packet.get("receiver_id")
        if (type(capture) is int and capture >= 0 and isinstance(boot, str) and boot
                and isinstance(receiver, str) and receiver):
            return name, (receiver, boot, capture), metadata.get("processed_monotonic")
    frame = metadata.get("frame_id")
    if kind == "frame" and isinstance(frame, str) and frame:
        return name, frame, metadata.get("processed_monotonic")
    return (name, None, metadata.get("processed_monotonic")) if include_unidentified else None


async def capture(session, url, token, window_seconds, ready=None):
    from aiohttp import ClientWSTimeout, WSMsgType

    stamps = {feed: [] for feed in FEEDS}
    received_counts = dict.fromkeys(FEEDS, 0)
    unidentified = dict.fromkeys(FEEDS, 0)
    seen = {feed: set() for feed in FEEDS}
    ages = {feed: [] for feed in FEEDS}
    observation = {"seconds": 0, "feeds": {}}
    try:
        async with session.ws_connect(url, headers={"Authorization": "Bearer " + token},
                                      timeout=ClientWSTimeout(ws_close=5),
                                      max_msg_size=0) as socket:
            start = time.monotonic()
            if ready is not None:
                ready.set()
            while (remaining := window_seconds - (time.monotonic() - start)) > 0:
                try:
                    message = await socket.receive(timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    observation["error"] = message.type.name
                    break
                if message.type == WSMsgType.BINARY:
                    record = feed_record(message.data, include_unidentified=True)
                    if record is not None:
                        feed, fingerprint, processed = record
                        received_counts[feed] += 1
                        if fingerprint is None:
                            unidentified[feed] += 1
                            continue
                        if fingerprint not in seen[feed]:
                            seen[feed].add(fingerprint)
                            received = time.monotonic()
                            stamps[feed].append(received - start)
                            if positive(processed) and processed <= received:
                                ages[feed].append(received - processed)
            elapsed = time.monotonic() - start
            observation["seconds"] = elapsed
            for feed, times in stamps.items():
                edges = [0.0, *times, elapsed]
                observation["feeds"][feed] = {
                    "records": len(times),
                    "received_records": received_counts[feed],
                    "unidentified_records": unidentified[feed],
                    "duplicate_records": received_counts[feed] - unidentified[feed] - len(times),
                    "max_gap_seconds": max(b - a for a, b in zip(edges, edges[1:])),
                    "max_delivery_age_seconds": max(ages[feed], default=None),
                    "unclocked_records": len(times) - len(ages[feed]),
                }
    except Exception as error:
        observation["error"] = getattr(error, "status", None) or type(error).__name__
    return observation


async def observe_streams(public, bridge, node_file, window_seconds, ready=None):
    from aiohttp import ClientSession, ClientTimeout

    external, local = urlsplit(public), urlsplit(bridge)
    if external.scheme != "wss" or external.username or external.password or not external.hostname:
        raise ValueError("Public socket requires a configured WSS endpoint")
    if (local.scheme != "ws" or local.hostname not in ("127.0.0.1", "::1", "localhost")
            or local.username or local.password):
        raise ValueError("Bridge socket requires a loopback WS endpoint")
    config = json.loads(Path(node_file).read_text())
    configured = urlsplit(config["stream_gateway_url"])
    if (configured.hostname, configured.port or 443) != (external.hostname, external.port or 443):
        raise ValueError("Public socket must match the configured stream gateway")
    token = hmac.new(config["stream_router_secret"].encode(), b"rover-site", "sha256").hexdigest()
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=10)) as session:
        # Keep observation time independent of the peer's close handshake.
        async def collect(label, url):
            async with asyncio.timeout(window_seconds + 15):
                signal = None if ready is None else ready[label]
                return await capture(session, url, token, window_seconds, signal)

        values = await asyncio.gather(*(collect(label, url) for label, url in zip(CONSUMERS, (public, bridge))),
                                      return_exceptions=True)
    return {key: {"seconds": 0, "error": type(value).__name__} if isinstance(value, Exception) else value
            for key, value in zip(CONSUMERS, values)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    observe = subcommands.add_parser("observe-streams")
    observe.add_argument("--public", required=True)
    observe.add_argument("--bridge", required=True)
    observe.add_argument("--node-file", required=True)
    verify = subcommands.add_parser("verify")
    for command in (observe, verify):
        command.add_argument("--window-seconds", type=float, required=True)
    verify.add_argument("--max-gap-seconds", type=float, required=True)
    verify.add_argument("--max-delivery-age-seconds", type=float, required=True)
    verify.add_argument("evidence", nargs="?", help="JSON path, or stdin when omitted")
    args = parser.parse_args(argv)
    if (not positive(args.window_seconds) or (args.command == "verify" and
            not all(map(positive, (args.max_gap_seconds, args.max_delivery_age_seconds))))):
        parser.error("Observation and continuity budgets must be positive finite seconds")
    if args.command == "observe-streams":
        print(json.dumps(asyncio.run(observe_streams(args.public, args.bridge, args.node_file, args.window_seconds))))
        return 0
    evidence = json.loads(Path(args.evidence).read_text()) if args.evidence else json.load(sys.stdin)
    failures = assess(evidence, args.window_seconds, args.max_gap_seconds, args.max_delivery_age_seconds)
    print(json.dumps({"passed": not failures, "failures": failures}))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
