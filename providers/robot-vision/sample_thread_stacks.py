#!/usr/bin/env python3
"""Bounded opt-in local stack collection; never starts or modifies the service."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

LIMITS = {"threads": 64, "frames_per_thread": 32, "json_bytes": 65536,
          "name_chars": 128, "filename_chars": 512, "function_chars": 128}
FIELDS = {"schema", "pid", "observation_started_monotonic_s",
          "observation_finished_monotonic_s", "frames_order", "threads_total",
          "threads_recorded", "limits", "truncated", "thread_limit_reached",
          "byte_limit_reached", "threads"}


def validate_snapshot(value, pid):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("unexpected_snapshot_shape")
    if (value["schema"] != "biome.vision.thread-stacks.v1" or value["pid"] != pid or
            value["frames_order"] != "innermost_first" or value["limits"] != LIMITS):
        raise ValueError("unexpected_snapshot_identity")
    for field in ("truncated", "thread_limit_reached", "byte_limit_reached"):
        if type(value[field]) is not bool:
            raise ValueError("unexpected_snapshot_flag")
    for field in ("threads_total", "threads_recorded"):
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError("unexpected_snapshot_count")
    if (not isinstance(value["threads"], list) or len(value["threads"]) > LIMITS["threads"] or
            value["threads_recorded"] != len(value["threads"]) or
            value["threads_total"] < value["threads_recorded"]):
        raise ValueError("unexpected_snapshot_count")
    for field in ("observation_started_monotonic_s", "observation_finished_monotonic_s"):
        if type(value[field]) not in (int, float) or not math.isfinite(value[field]):
            raise ValueError("unexpected_snapshot_time")
    if value["observation_finished_monotonic_s"] < value["observation_started_monotonic_s"]:
        raise ValueError("unexpected_snapshot_time")
    for thread in value["threads"]:
        if (not isinstance(thread, dict) or set(thread) !=
                {"ident", "name", "frames", "frames_truncated", "strings_truncated"} or
                type(thread["ident"]) is not int or thread["ident"] <= 0 or
                type(thread["frames_truncated"]) is not bool or
                type(thread["strings_truncated"]) is not bool):
            raise ValueError("unexpected_thread_shape")
        if thread["name"] is not None and (not isinstance(thread["name"], str) or
                                          len(thread["name"]) > LIMITS["name_chars"]):
            raise ValueError("unexpected_thread_name")
        if not isinstance(thread["frames"], list) or len(thread["frames"]) > LIMITS["frames_per_thread"]:
            raise ValueError("unexpected_frame_count")
        for frame in thread["frames"]:
            if (not isinstance(frame, dict) or set(frame) != {"filename", "function", "lineno"} or
                    type(frame["lineno"]) is not int or frame["lineno"] < 0):
                raise ValueError("unexpected_frame_shape")
            for key, maximum in (("filename", LIMITS["filename_chars"]),
                                 ("function", LIMITS["function_chars"])):
                if frame[key] is not None and (not isinstance(frame[key], str) or len(frame[key]) > maximum):
                    raise ValueError("unexpected_frame_text")
    if len(json.dumps(value, allow_nan=False).encode()) > LIMITS["json_bytes"]:
        raise ValueError("unexpected_snapshot_size")
    return value


async def request_snapshot(path):
    reader, writer = await asyncio.open_unix_connection(str(path), limit=8192)
    try:
        body = json.dumps({"operation": "robot.vision.status",
                           "inputs": {"receiver_only": True, "thread_stacks": True}}).encode()
        writer.write(b"POST /v1/query HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                     b"Content-Type: application/json\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > 8192:
            raise ValueError("response_header_too_large")
        lines = header.decode("ascii").split("\r\n")
        if lines[0].split()[1:2] != ["200"]:
            raise ValueError("response_not_ok")
        lengths = [line.split(":", 1)[1].strip() for line in lines[1:]
                   if line.lower().startswith("content-length:")]
        if (len(lengths) != 1 or not lengths[0].isdigit() or
                any(line.lower().startswith("transfer-encoding:") for line in lines[1:])):
            raise ValueError("response_length_invalid")
        size = int(lengths[0])
        if not 0 < size <= LIMITS["json_bytes"]:
            raise ValueError("response_body_too_large")
        value = json.loads(await reader.readexactly(size))
        return value, size
    finally:
        # Closing the local connection is nonblocking, including cancellation.
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), .05)
        except (TimeoutError, OSError):
            pass


async def collect(args):
    rows = []
    started = time.monotonic()
    deadline = started + args.duration
    next_start = started
    for index in range(100):
        remaining = deadline - time.monotonic()
        delay = max(0.0, next_start - time.monotonic())
        if remaining <= delay + .05:
            break
        if delay:
            await asyncio.sleep(delay)
        before = time.monotonic()
        remaining = deadline - before - .05
        if remaining <= 0:
            break
        next_start = before + args.interval
        row = {"event": "snapshot", "index": index, "request_started_monotonic_s": before}
        try:
            value, size = await asyncio.wait_for(request_snapshot(args.socket),
                                                 min(args.timeout, remaining))
            after = time.monotonic()
            validate_snapshot(value, args.expected_pid)
            if not (before <= value["observation_started_monotonic_s"] <=
                    value["observation_finished_monotonic_s"] <= after):
                raise ValueError("snapshot_outside_request")
            row.update(response_bytes=size, snapshot=value, status="ok")
        except TimeoutError:
            row.update(status="timeout")
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            row.update(status="invalid_or_unavailable")
        row["request_finished_monotonic_s"] = time.monotonic()
        row["elapsed_s"] = row["request_finished_monotonic_s"] - before
        rows.append(row)
        if row["status"] != "ok":
            # Do not accumulate abandoned queries behind a blocked interpreter.
            break
    return rows, started, time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--service-path", type=Path, required=True)
    parser.add_argument("--expected-service-sha256", required=True)
    parser.add_argument("--expected-helper-sha256", required=True)
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--duration", type=float, default=20.)
    parser.add_argument("--interval", type=float, default=.2)
    parser.add_argument("--timeout", type=float, default=.5)
    args = parser.parse_args()
    if not (0 < args.duration <= 20 and .2 <= args.interval <= 20 and
            0 < args.timeout <= 1 and args.expected_pid > 1):
        parser.error("require duration<=20s, interval>=0.2s, timeout<=1s and expected PID")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", args.operation):
        parser.error("invalid operation")
    helper = args.service_path.with_name("receiver_thread_stacks.py")
    expected = {args.service_path: args.expected_service_sha256, helper: args.expected_helper_sha256}
    for path, digest in expected.items():
        if (not re.fullmatch(r"[0-9a-f]{64}", digest) or path.is_symlink() or
                hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise SystemExit("source_guard_failed")
    if not args.socket.is_socket():
        raise SystemExit("local_socket_missing")
    os.umask(0o077)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    rows = []
    started = finished = time.monotonic()
    try:
        rows, started, finished = asyncio.run(collect(args))
    finally:
        # Buffered output avoids file writes during the sampling interval.
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps({"event": "provenance", "operation": args.operation,
                                     "expected_pid": args.expected_pid,
                                     "source_sha256": {str(p): h for p, h in expected.items()},
                                     "duration_limit_s": args.duration, "interval_s": args.interval,
                                     "timeout_s": args.timeout, "maximum_samples": 100,
                                     "started_monotonic_s": started,
                                     "finished_monotonic_s": finished}) + "\n")
            for row in rows:
                stream.write(json.dumps(row, allow_nan=False) + "\n")
    summary = {"event": "sampling_complete", "operation": args.operation,
               "path": str(args.output), "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
               "samples": len(rows), "accepted": sum(row["status"] == "ok" for row in rows),
               "elapsed_s": finished-started,
               "source_unchanged": all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                                       for path, digest in expected.items())}
    print(json.dumps(summary), flush=True)
    return 0 if rows and all(row["status"] == "ok" for row in rows) and summary["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
