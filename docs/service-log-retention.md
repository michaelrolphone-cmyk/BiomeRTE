# Opt-in Mac service text-log retention

`deploy/service_log_retention.py` is a read-only planner and an explicitly invoked
rotator. It does not install a scheduled job, alter service plists, signal/restart
services, or change their main PID. Native deployment and scheduling are separate
coordinated steps. In particular, do not reload the maintenance service while its
commands, including VNC work, are active.

## Scope

Plan from verified installed `org.biomerte.*` LaunchAgent/LaunchDaemon plists.
The actual StandardOutPath and StandardErrorPath values determine the targets;
there is no hardcoded provider list. Shared stdout/stderr paths are deduplicated.
Only `.log`/`.err` files under the owner's `.local/share/biome*` service directories
are admitted. Command/job/artifact/workspace directories, SQLite/other record
stores, retention's own state, `/dev/null`, and unrelated user/VNC jobs are excluded.
A changed plist invalidates the approved manifest. Adding a service or changing
limits requires another reviewed plan, not silent expansion of the target set.

Example preparation, substituting the actual installed plist paths:

```sh
python3 -B -S deploy/service_log_retention.py plan \
  --home /Users/owner --plists /Users/owner/Library/LaunchAgents/org.biomerte.controller.plist \
  --max-bytes 1048576 --generations 2 > service-log-manifest.json
python3 -B -S deploy/service_log_retention.py digest --manifest service-log-manifest.json
```

The later, explicitly authorized invocation requires that exact digest:

```sh
python3 -B -S deploy/service_log_retention.py apply \
  --manifest service-log-manifest.json --expected-sha256 APPROVED_DIGEST
```

Do not run apply merely to inspect a plan. It writes private archives and can
truncate admitted diagnostic logs. It must run as the existing log-owning user.
No sudo, new credentials, or system-wide log settings are used.

## Historical preservation

The first apply saves a full historical byte prefix from every admitted file,
recording path, device/inode, byte watermark and SHA-256. It fsyncs and re-reads
the archive, and verifies the same source prefix. Every baseline must be verified
before **any** source log can be truncated. Baseline files are never pruned.
A partial baseline commit, corrupt archive, changed source identity, symlink,
hard link, unexpected owner, or group/world-writable source/state fails closed.
Baseline corruption does not authorize reconstructing it from an already rolled
log. Reconcile the preserved archive rather than deleting it to make a retry pass.

The baseline lives under `~/.local/share/biomerte-service-log-retention`, in a
private per-path directory. Directories are 0700 and new files 0600. Originals
keep their paths, inodes, owners, and permissions. A single lock prevents
competing maintenance runs. An existing state directory binds to one exact
manifest; do not overwrite that binding to change scope or retention limits.

## Rolling window and limits

Defaults are a 1 MiB rotation threshold and two recent managed generations per
sink. An oversized generation retains only the newest 1 MiB of bytes. This can
start mid-line or mid-UTF-8 sequence; these are byte-bounded debugging copies,
not structured records or complete transaction histories. Each generation has
its byte count, SHA-256, sequence and source watermark recorded.

For N sinks, B bytes and G generations, committed rolling payloads occupy at
most N × B × G. One fixed bounded staging file per sink can add at most N × B
after interrupted runs. Fixed slot names avoid unbounded orphan growth. Baseline
archives are fixed historical data outside this future rolling budget. Current
source logs can grow between runs; an operator may schedule the approved command
once a minute, with scheduler stdout/stderr disabled and the bounded status file
used for monitoring. No schedule is installed by this change.

This is not an instantaneous disk ceiling. Scheduler downtime, unsafe writers,
permission failures, or a burst between passes can exceed the active-log
threshold. If a hard writer-time cap is required, the producers need their own
rolling writers; periodic external rotation cannot provide it.

## Writer checks and unavoidable copy-truncate race

Before truncation the rotator uses native `lsof -nP +fg -- PATH` to inspect all
visible descriptors for the selected inode, not only a service's main PID or
stdout/stderr. Every writable descriptor must show AP (O_APPEND). Unknown access
modes, memory mappings, mismatched identities, warnings, command errors or
non-append writers block that source. Read-only descriptors do not block it.
Checks are repeated after copying and immediately before truncation. Source
paths are resolved through pinned no-follow directory descriptors and checked
again against the opened inode. Non-append writers must not be "fixed" by this
script, and services are never restarted to make the guard pass.

**O_APPEND does not make copy-truncate lossless.** A concurrent append after the
last captured watermark but before ftruncate may be lost. There is no cooperative
writer lock, so the interval cannot be eliminated. Verified historical baseline
bytes remain preserved; future diagnostics in that race are not guaranteed. A
safe writer check also depends on lsof having complete visibility of all writers.
The same-user, owner-writable-only policy limits ordinary writers, but invisible
privileged processes cannot be ruled out solely by unprivileged inspection.
Native rollout must qualify lsof visibility and parsing for the actual host.

The rotator overwrites only its own bounded managed slots. It never renames an
active source log or purges its baseline, operational journals, active command
outputs, delivery/replay fences, or pre-existing unrelated files.

## Qualification status

Local tests cover historical archive verification, fixed byte/count bounds,
streaming oversized lines, source replacement, unsafe writers, missing append
flags, incomplete lsof output, symlinks/hard links, owner/permission checks,
manifest drift, competing rotators, and crash/disk-full failures before baseline
commit. Linux tests inject writer evidence; they do not constitute native Mac
qualification or deployment. No existing host service has been modified.
