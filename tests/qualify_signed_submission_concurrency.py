"""Qualify a deployed Site with sixteen independent, nonphysical core.echo calls.

On an enrolled Mac, run python -m tests.qualify_signed_submission_concurrency.
The JSON report preserves each first response, durable action, and lease. Reuse
--run-id to inspect the same operations during incident recovery.
"""
import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from tests.run_live_provider_recovery import LOCAL, Site, release_binding, save, timed_call


async def submit(site, operation):
    receipt, action = await timed_call(site, "cap.submit", {
        "contract": "core.echo:1", "operation": operation, "inputs": {"value": operation},
    }, operation + "-submit")
    return dict(receipt, operation=operation, matches=action.get("id") == operation,
                state=action.get("state"), lease=action.get("lease"))


async def status(site, operation):
    receipt, action = await timed_call(site, "cap.status", {"operation": operation}, uuid.uuid4().hex)
    return dict(receipt, matches=action.get("id") == operation, state=action.get("state"),
                lease=action.get("lease"), echo=action.get("result") == {"value": operation})


async def lease_status(site, lease, operation):
    receipt, row = await timed_call(site, "cap.lease", {"lease": lease}, uuid.uuid4().hex)
    return dict(receipt, matches=row.get("id") == lease and row.get("task") == operation,
                state=row.get("state"))


def complete(read, state):
    return bool(read and read.get("status") == 200 and read.get("matches") is True
                and read.get("state") == state)


def all_jobs_passed(report):
    run_id = report["run_id"]
    first = report.get("submissions", [])
    progress = report.get("progress", [])
    results = report.get("readback", [])
    if len(first) != 16 or len(results) != 16 or len(progress) != 2:
        return False
    seed = report.get("baseline", {})
    progress_lease = seed.get("lease") or report.get("progress_lease")
    if not (progress_lease and progress[0].get("lease") == progress_lease
            and complete(progress[0], "succeeded") and complete(progress[1], "released")):
        return False
    for index, (submitted, result) in enumerate(zip(first, results)):
        operation = f"{run_id}-{index:02d}"
        action = result["action"]
        if not (submitted.get("operation") == result.get("operation") == operation
                and submitted.get("request_id") == operation + "-submit"
                and submitted.get("status") == 200 and submitted.get("matches") is True
                and submitted.get("lease") and submitted["lease"] == action.get("lease")
                and complete(action, "succeeded") and action.get("echo") is True
                and complete(result.get("release"), "released")):
            return False
    return True


async def readback(site, operation, initial_lease=None, observe=None):
    action = await status(site, operation)
    if observe:
        observe(operation + "/action", action)
    lease = action["lease"] or initial_lease
    released = await lease_status(site, lease, operation) if lease else None
    if observe and released:
        observe(operation + "/lease", released)
    return dict(operation=operation, action=action, release=released)


async def qualify(site, run_id, progress_operation=None, progress_lease=None, stagger=.035, observe=None):
    report = dict(run_id=run_id, passed=False)

    async def recorded(operation):
        if observe:
            observe(operation + "/started", True)
        result = await submit(site, operation)
        if observe:
            observe(operation + "/submission", result)
        return result

    if progress_operation is None:
        progress_operation = run_id + "-baseline"
        seed = report["baseline"] = await recorded(progress_operation)
        progress_lease = seed["lease"]
        if seed["status"] != 200 or not seed["matches"] or not progress_lease:
            return report

    report["progress_lease"] = progress_lease

    async def staggered(index):
        await asyncio.sleep(index * stagger)
        return await recorded(f"{run_id}-{index:02d}")

    async def progress():
        await asyncio.sleep(7 * stagger)
        action, lease = await asyncio.gather(status(site, progress_operation),
                                            lease_status(site, progress_lease, progress_operation))
        if observe:
            observe(progress_operation + "/action", action)
            observe(progress_operation + "/lease", lease)
        return [action, lease]

    submissions, previous = await asyncio.gather(
        asyncio.gather(*(staggered(i) for i in range(16))), progress())
    report.update(submissions=submissions, progress=previous)
    limit = asyncio.Semaphore(4)

    async def bounded(first):
        async with limit:
            return await readback(site, first["operation"], first["lease"], observe)

    report["readback"] = await asyncio.gather(*(bounded(item) for item in submissions))
    report["passed"] = all_jobs_passed(report)
    return report


async def qualify_release(site, node, run_id, progress_operation, progress_lease):
    from aiohttp import ClientError, ClientSession, ClientTimeout

    origin = "https://" + urlsplit(node["stream_gateway_url"]).netloc
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        report = dict(run_id=run_id, passed=False)
        try:
            before = await release_binding(session, origin, node["node"])
            report = await qualify(site, run_id, progress_operation, progress_lease)
            report["placement"] = before
            report["final_placement"] = await release_binding(session, origin, node["node"])
            report["passed"] = report["passed"] and report["final_placement"] == before
        except (ClientError, TimeoutError, ValueError) as error:
            report.update(passed=False, placement_error=type(error).__name__)
        return report


def pressure_directory(state, placement):
    identity = json.dumps(placement, sort_keys=True, separators=(",", ":")).encode()
    return state / "release-pressure" / hashlib.sha256(identity).hexdigest()


def import_verified_pressure(state, placement, report):
    """Adopt an already observed full wave under the monitor's process lock."""
    if (not placement or report.get("placement") != placement or report.get("final_placement") != placement
            or report.get("passed") is not True or not all_jobs_passed(report)):
        raise ValueError("pressure evidence does not qualify this placement")
    folder = pressure_directory(state, placement)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "first.json"
    if target.is_file() and json.loads(target.read_text()) == report:
        return
    if target.exists() or (folder / "started.json").exists():
        raise ValueError("the original release pressure attempt already exists")
    save(target, report)


async def scheduled_pressure(state, placement, site, current_binding):
    """One fresh wave per exact witnessed placement, with read-only crash recovery."""
    folder = pressure_directory(state, placement)
    folder.mkdir(parents=True, exist_ok=True)
    first = folder / "first.json"
    started = folder / "started.json"
    if not first.exists() and not started.exists():
        run_id = "scheduled-pressure-" + uuid.uuid4().hex
        journal = dict(run_id=run_id, placement=placement, receipts={})
        save(started, journal)
        def observe(key, result):
            journal["receipts"][key] = result
            save(started, journal)
        report = dict(run_id=run_id, placement=placement, passed=False)
        try:
            report.update(await qualify(site, run_id, observe=observe))
            report["final_placement"] = await current_binding()
            report["passed"] = report["passed"] and report["final_placement"] == placement
        except Exception as error:
            report.update(passed=False, observation_error=type(error).__name__)
        save(first, report)
    elif not first.exists():
        journal = json.loads(started.read_text())
        save(first, dict(run_id=journal["run_id"], placement=placement, passed=False,
                         observation_error="interrupted", partial_receipts=journal["receipts"]))
    report = json.loads(first.read_text())
    passed = (report.get("passed") is True and report.get("placement") == report.get("final_placement") == placement
              and all_jobs_passed(report))
    return dict(passed=passed, run_id=report["run_id"], receipt=str(first.relative_to(state)))


async def reconcile_pressure(state, placement, site):
    """Read original identities once after an incomplete wave, without replay."""
    folder = pressure_directory(state, placement)
    first = folder / "first.json"
    observed = folder / "reconciled.json"
    if not first.exists() or observed.exists():
        return
    report = json.loads(first.read_text())
    if report.get("passed"):
        return
    run_id = report["run_id"]
    settled = {row["operation"] for row in report.get("readback", [])
               if complete(row.get("action"), "succeeded") and complete(row.get("release"), "released")}
    progress = report.get("progress", [])
    if len(progress) == 2 and complete(progress[0], "succeeded") and complete(progress[1], "released"):
        settled.add(run_id + "-baseline")
    journal = json.loads((folder / "started.json").read_text())
    original = {key.removesuffix("/started") for key in journal["receipts"] if key.endswith("/started")}
    limit = asyncio.Semaphore(4)
    async def bounded(operation):
        async with limit:
            return await readback(site, operation)
    rows = await asyncio.gather(*(bounded(operation) for operation in sorted(original - settled)))
    save(observed, dict(run_id=run_id, placement=placement, readback=rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-home", type=Path, default=Path.home() / ".local/share/biome-mac-terminal")
    parser.add_argument("--node-file", type=Path, default=LOCAL / "biomerte-controller/node.json")
    parser.add_argument("--run-id", default="signed-pressure-" + uuid.uuid4().hex)
    parser.add_argument("--progress-operation", help="Previously completed operation for the concurrent status read")
    parser.add_argument("--progress-lease", help="Its exact released lease")
    args = parser.parse_args()
    if bool(args.progress_operation) != bool(args.progress_lease):
        parser.error("--progress-operation and --progress-lease must be specified together")
    node = json.loads(args.node_file.read_text())
    report = asyncio.run(qualify_release(Site(args.gateway_home), node, args.run_id,
                                         args.progress_operation, args.progress_lease))
    print(json.dumps(report))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
