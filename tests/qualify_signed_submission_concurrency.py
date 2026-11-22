"""Qualify a deployed Site with sixteen independent, nonphysical core.echo calls.

On an enrolled Mac, run python -m tests.qualify_signed_submission_concurrency.
The JSON report preserves each first response, durable action, and lease. Reuse
--run-id to inspect the same operations during incident recovery.
"""
import argparse
import asyncio
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from tests.run_live_provider_recovery import LOCAL, Site, release_binding, timed_call


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
    return read["status"] == 200 and read["matches"] and read["state"] == state


async def qualify(site, run_id, progress_operation=None, progress_lease=None, stagger=.035):
    report = dict(run_id=run_id, passed=False)
    if progress_operation is None:
        progress_operation = run_id + "-baseline"
        seed = report["baseline"] = await submit(site, progress_operation)
        progress_lease = seed["lease"]
        if seed["status"] != 200 or not seed["matches"] or not progress_lease:
            return report

    async def staggered(index):
        await asyncio.sleep(index * stagger)
        return await submit(site, f"{run_id}-{index:02d}")

    async def progress():
        await asyncio.sleep(7 * stagger)
        return await asyncio.gather(status(site, progress_operation),
                                   lease_status(site, progress_lease, progress_operation))

    submissions, previous = await asyncio.gather(
        asyncio.gather(*(staggered(i) for i in range(16))), progress())
    report.update(submissions=submissions, progress=previous)
    limit = asyncio.Semaphore(4)

    async def readback(first):
        async with limit:
            operation = first["operation"]
            action = await status(site, operation)
            lease = action["lease"] or first["lease"]
            released = await lease_status(site, lease, operation) if lease else None
            return dict(operation=operation, action=action, release=released)

    results = report["readback"] = await asyncio.gather(*(readback(item) for item in submissions))
    report["passed"] = (all(item["status"] == 200 and item["matches"] for item in submissions)
                        and complete(previous[0], "succeeded") and complete(previous[1], "released")
                        and all(complete(row["action"], "succeeded") and row["action"]["echo"]
                                and row["release"] and complete(row["release"], "released")
                                for row in results))
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
