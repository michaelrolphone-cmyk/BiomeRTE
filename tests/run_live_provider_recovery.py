"""Run a single-flight acceptance check through the deployed Site and Rover sockets."""

import argparse
import asyncio
import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from tests.qualify_live_provider_recovery import CONSUMERS, assess, observe_streams, positive

LOCAL = Path.home() / ".local/share"
PUBLIC_FAILURES = frozenset({
    "Managed controller is busy", "Controller runtime reply unavailable",
    "Controller runtime unavailable", "Invalid controller runtime result",
    "Logical service authority is unavailable", "Declared logical service is unavailable",
})
TERMINAL = {"succeeded", "failed", "cancelled", "abandoned", "denied", "uncertain"}


def save(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as out:
        json.dump(data, out, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


class Site:
    """Use the installed Mac Site transport's OAuth rotation and exact single HTTP attempt."""

    def __init__(self, home):
        spec = importlib.util.spec_from_file_location("qualification_gateway", home / "mac_provider.py")
        self.gateway = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gateway)

    async def call(self, operation, arguments, request_id):
        return await asyncio.to_thread(self._request, operation, arguments, request_id)

    def _request(self, operation, arguments, request_id):
        g = self.gateway
        args = dict(operation=operation, arguments=arguments, realm="site", request_id=request_id)
        rpc = dict(jsonrpc="2.0", id=request_id, method="tools/call",
                   params=dict(name="biome_invoke", arguments=args))
        try:
            reply = g.request(g.RESOURCE, rpc, headers={"Authorization": "Bearer " + g.credentials()["access_token"]})
            if reply.get("id") != request_id or "error" in reply:
                return "protocol_error", {}
            result = reply["result"]
            value = result.get("structuredContent")
            if value is None and not result.get("isError"):
                value = json.loads(result["content"][0]["text"])
            if result.get("isError"):
                value = value if isinstance(value, dict) else {}
                category = "signed_runtime" if value.get("error_code") == "CONTROLLER_RUNTIME_UNAVAILABLE" else "site_error"
                content = result.get("content")
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    detail = content[0].get("text")
                    if isinstance(detail, str) and detail in PUBLIC_FAILURES:
                        category = detail
                diagnostics = {"error_category": category}
                if type(value.get("retry_same_id")) is bool:
                    diagnostics["retry_same_id"] = value["retry_same_id"]
                status = value.get("status")
                return (status if type(status) is int and 400 <= status < 600 else "site_error"), diagnostics
            if not isinstance(value, dict):
                return "invalid_result", {}
            return 200, value
        except Exception as error:
            status = getattr(error, "status", None)
            if type(status) is int and 400 <= status < 600:
                return status, {"error_category": "http_transport"}
            return type(error).__name__, {}


async def timed_call(site, name, arguments, request_id):
    started_at = time.time()
    start = time.monotonic()
    status, value = await site.call(name, arguments, request_id)
    receipt = dict(status=status, started_at=started_at, elapsed_seconds=time.monotonic() - start,
                   request_id=request_id)
    if status != 200:
        receipt.update({key: value[key] for key in ("error_category", "retry_same_id") if key in value})
    return receipt, value


async def collect_action(site, operation, window, interval=3):
    deadline = time.monotonic() + window
    submission, value = await timed_call(site, "cap.submit", dict(operation=operation,
                                              contract="robot.vision.metadata:1", inputs={}), operation + "-submit")
    submission.update(operation=operation, lease=value.get("lease"))
    if submission["status"] == 200 and value.get("id") != operation:
        submission["status"] = "wrong_operation"
    evidence = dict(submission=submission, results=[], lease_check={})
    lease = submission["lease"]
    delay = interval
    while time.monotonic() < deadline:
        check, action = await timed_call(site, "cap.status", {"operation": operation}, uuid.uuid4().hex)
        result = action.get("result")
        metadata = result.get("result") if isinstance(result, dict) else None
        valid_result = (isinstance(metadata, dict) and not metadata.get("error")
                        and isinstance(metadata.get("camera"), dict))
        check.update(operation=action.get("id"), lease=action.get("lease"),
                     state=action.get("state"), has_result=valid_result)
        evidence["results"].append(check)
        if action.get("id") == operation and action.get("state") in TERMINAL:
            lease = lease or action.get("lease")
            break
        await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
        delay = interval if check["status"] == 200 else min(15, delay * 2)
    if isinstance(lease, str) and lease:
        check, row = await timed_call(site, "cap.lease", {"lease": lease}, uuid.uuid4().hex)
        check.update(operation=row.get("task"), lease=row.get("id"), state=row.get("state"))
        evidence["lease_check"] = check
    return evidence


async def release_binding(session, origin, alias):
    async with session.post(origin + "/api/service-authorities/resolve", json={"controller": alias},
                            allow_redirects=False) as response:
        response.raise_for_status()
        value = await response.json()
    binding = value.get("binding") or {}
    fields = ("service_id", "manifest_revision", "worker_id", "boot_epoch", "worker_incarnation")
    if not (value.get("authority_kind") == "controller" and value.get("effect_ready") is True
            and value.get("state") == "active" and alias in [value.get("authority"), *value.get("controller_aliases", [])]
            and binding.get("grant_token") and all(binding.get(key) is not None for key in fields)):
        raise ValueError("no_active_placement")
    return dict(((key, binding[key]) for key in fields),
                grant_digest=hashlib.sha256(str(binding["grant_token"]).encode()).hexdigest())


async def monitor(args):
    from aiohttp import ClientSession, ClientTimeout
    from tests.qualify_signed_submission_concurrency import pressure_directory, reconcile_pressure, scheduled_pressure

    state = args.state_dir
    latest = state / "latest.json"
    prior = json.loads(latest.read_text()) if latest.exists() else {}
    report = dict(started=time.time(), passed=False, failures=[], placement=None, evidence={})
    measurement = state / "last-measurement.json"
    if prior.get("evidence") and not measurement.exists():
        save(measurement, prior)
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        try:
            node = json.loads(args.node_file.read_text())
            router = urlsplit(node["stream_gateway_url"])
            if router.scheme != "wss" or router.username or router.password or not router.hostname:
                raise ValueError("invalid_configured_gateway")
            public = "wss://" + router.netloc + "/api/live/vision"
            origin = "https://" + router.netloc
            placement = await release_binding(session, origin, node["node"])
            report["placement"] = placement
            if (not args.force and prior.get("signed_pressure") and placement == prior.get("placement")
                    and time.time() - (prior.get("finished") or 0) < (3600 if prior["passed"] else 900)):
                print(json.dumps(dict(reused=True, passed=prior["passed"], finished=prior["finished"])))
                return int(not prior["passed"])
            if latest.exists():
                save(state / "previous.json", prior)
            save(latest, dict(report, failures=["observation in progress"]))
            pending = state / "pending.json"
            recovering = pending.exists()
            operation = json.loads(pending.read_text())["operation"] if recovering else "provider-qualification-" + uuid.uuid4().hex
            site = Site(args.gateway_home)
            ready = {consumer: asyncio.Event() for consumer in CONSUMERS}

            async def await_connections():
                try:
                    async with asyncio.timeout(12):
                        await asyncio.gather(*(signal.wait() for signal in ready.values()))
                except TimeoutError:
                    report["failures"].append("both consumers had not connected before submission")

            connections = asyncio.create_task(await_connections())

            async def observe_action():
                await connections
                if not recovering:
                    save(pending, {"operation": operation})
                return await collect_action(site, operation, args.window_seconds)

            async def observe_pressure():
                await connections
                # Reconciliation runs on a later observation and never reissues a submit.
                try:
                    folder = pressure_directory(state, placement)
                    previous_attempt = (folder / "started.json").exists()
                    result = await scheduled_pressure(state, placement, site,
                        lambda: release_binding(session, origin, node["node"]))
                    if previous_attempt and not result["passed"]:
                        await reconcile_pressure(state, placement, site)
                    return result
                except Exception as error:
                    return dict(passed=False, observation_error=type(error).__name__)

            action, streams, pressure = await asyncio.gather(observe_action(),
                observe_streams(public, args.bridge, args.node_file, args.window_seconds, ready=ready),
                observe_pressure())
            report["signed_pressure"] = pressure
            if not pressure["passed"]:
                report["failures"].append("signed concurrency qualification failed; see first release pressure receipt")
            report["evidence"] = dict(action, streams=streams)
            report["failures"].extend(assess(report["evidence"], args.window_seconds,
                                             args.max_gap_seconds, args.max_delivery_age_seconds))
            if recovering:
                report["failures"].append("reconciliation of a previous operation requires a fresh acceptance run")
            release = action["lease_check"]
            # If a completed read-only operation already lost its lease row to
            # retention, this run lacks release proof. A later fresh operation is safe.
            result_returned = any(row.get("status") == 200 and row.get("operation") == operation
                                  and row.get("state") in TERMINAL and row.get("lease")
                                  for row in action["results"])
            expired = result_returned and release.get("status") in (404, 410)
            rejected = action["submission"].get("status") in (400, 401, 403, 422)
            if expired or rejected or (release.get("status") == 200 and release.get("operation") == operation and release.get("lease")
                    and release.get("state") == "released"):
                pending.unlink()
            if await release_binding(session, origin, node["node"]) != placement:
                report["failures"].append("active controller placement changed during observation")
        except Exception as error:
            if report["placement"] is None and prior:
                save(state / "previous.json", prior)
            status = getattr(error, "status", None)
            category = "HTTP " + str(status) if type(status) is int else type(error).__name__
            report["failures"].append("production observation failed (" + category + ")")
    report["finished"] = time.time()
    report["passed"] = not report["failures"]
    if report["evidence"]:
        save(measurement, report)
    save(latest, report)
    print(json.dumps(report))
    return int(not report["passed"])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--node-file", type=Path, default=LOCAL / "biomerte-controller/node.json")
    p.add_argument("--gateway-home", type=Path, default=LOCAL / "biome-mac-terminal")
    p.add_argument("--bridge", default="ws://127.0.0.1:8093/api/live/vision")
    p.add_argument("--state-dir", type=Path, default=LOCAL / "biomerte-provider-qualification")
    p.add_argument("--window-seconds", type=float, default=80)
    p.add_argument("--max-gap-seconds", type=float, default=1)
    p.add_argument("--max-delivery-age-seconds", type=float, default=1)
    p.add_argument("--force", action="store_true", help="fresh observation even inside the recheck interval")
    a = p.parse_args()
    if not all(map(positive, (a.window_seconds, a.max_gap_seconds, a.max_delivery_age_seconds))):
        p.error("budgets must be positive finite seconds")
    os.umask(0o077)
    a.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (a.state_dir / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps(dict(passed=False, failures=["qualification already running"])))
            return 1
        # A wedged installed OAuth process cannot strand this scheduler or start duplicate probes.
        signal.alarm(int(a.window_seconds) + 300)
        try:
            return asyncio.run(monitor(a))
        finally:
            signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
