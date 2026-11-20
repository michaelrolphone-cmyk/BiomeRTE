# Bound unchanged worker health publication

## Problem and evidence

The installed Pi FM and Docker workers use worker.py SHA256
acdc1e715c3c2757540c287cb53d407b179db6811fa168cb5916678df8821dde.
FM is configured to poll every 0.1 seconds with parallelism4. It reports ready,
pending0 and an explicitly retained historical uncertain operation. Docker polls
every1second with parallelism4 and retains two uncertain actions returning409.

A 60.004-second trace recorded172 FM health-file replacements and96 Docker
health-file replacements:537 of813 filesystem sync entries. Their logical
payload was108730bytes, while exact journal extents received46784512 of58556416
physical write bytes. Shared transactions do not permit assigning those journal
bytes to workers in proportion to their sync counts. This is an observed write
amplification mechanism, not a claim that all disk writes have one cause.

Native provider.poll selects only fencing leases; cap.fenced changes them to
bound. Repeated already acknowledged fences are not the supported FM diagnosis.

## Publication contract

Worker polling and execution stay at their configured cadence. last_health is
refreshed in memory after each completed poll. The first health publication,
every semantic change and a missing or changed regular target are published
immediately. Unchanged safe snapshots are published when either monotonic or
wall elapsed time reaches5seconds. A reversal of either clock also publishes.

The persisted observed_at is the observation actually published, not a fabricated
per-poll timestamp. With unchanged health, publication is due at5seconds and occurs
on the next completed poll. Slow or blocked polls can extend that interval, as
they already could prevent health publication before this change. Semantic
changes are never intentionally delayed by the heartbeat.

The cache holds canonical serialized semantics, so in-place mutations of nested
blocked errors or historical debt cannot silently modify the cached comparison.
Before an unchanged skip, the existing target must pass the original regular-file
and single-link safety checks and match the last published device/inode, mode,
link count, size, mtime_ns and ctime_ns. Symlinks, hardlinks and FIFOs fail closed.
The cache is invalidated before a write and populated only after the existing file
fsync, atomic replacement and directory fsync all succeed. A failed publication
is retried even if the replacement became visible before a directory fsync failed.

This telemetry cache is not a durable ACK, custody record, recovery marker,
execution proof or permission to release/replay an operation. Those paths and
SQLite FULL durability are unchanged.

## Reader review and freshness tradeoff

The review scanned293 production and documentation files at main
c76d819629fd388d9a60cefc8bb0f1d6e801b467: biomerte, deploy excluding the vendored
transport, providers, integrations and docs. No worker-health.json freshness
reader or cutoff was found within that bounded coverage. This is not proof that
no external consumer exists. deploy/gateway-agent.py obtains consumer_health from
the controller's system.progress RPC, not from worker-health.json. The repository
documents the local worker file as execution telemetry separate from transport
health and controller failover readiness.

Five seconds is an explicit bounded telemetry-freshness tradeoff approved for
this repair, not a discovered universal reader threshold. The pre-existing Mac
terminal transport heartbeat uses a different file and is not used to infer a
worker-health cutoff.

## Compatibility and deployment boundary

This branch builds on the reviewed PR131 pin
9987470ea08c59e099675d8dd82c69c6b6f5f604 and retains its five-line ActionPool
correction: retries of the same failed kind/id/generation continue but no longer
request a fast follow-up as new progress. New work and fences remain prompt.

The runtime artifact for FM/Docker is ONLY biomerte/worker.py, derived from the
actual installed worker plus that PR131 correction and this publication change.
The compatibility branch contains an earlier supervised_commands.py publication
fix; applying that module to FM/Docker is not part of this change and is not a
dependency of this code. Qualification must also use the actual installed
baseline archive with ONLY worker.py replaced. The controller and all existing
action, fence, custody and recovery state remain outside this source change.

The focused tests use real files and syncs, clock controls, unsafe targets and
failure injection. They cover first publication, fast unchanged polling,5second
heartbeats, nested changes, provider/pending/error transitions, both clock
directions, deletion, inode replacement, in-place modification, failed file and
directory syncs, a new worker instance, and the real0.1second idle run loop.
Existing PR131 ActionPool tests retain retry, uncertainty, serialization and
new-work coverage. CI does not establish live deployment or outage stability.
