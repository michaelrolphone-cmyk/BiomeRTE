# Current source and deployment provenance

`main` is the canonical integrated source. The November 4, 2026 consolidation
includes controller replication/failover, generic capability continuations,
named agent events, the terminal execution-policy fixes, Pi/Mac Docker and
storage providers, registry deletion, and the deployed LM Studio coding tools.
It incorporates the histories represented by PRs 1–10 rather than requiring
operators to assemble those branches.

## What the live comparison established

Both controllers selected release `2f39de8ce442000063410938be85827962a884d8`.
All 28 Python files in their `biomerte` and `deploy` directories matched, with
combined SHA-256 `e7921c33ef908eeafe767941e246c742094394b383075c1c78275cafa24c6afa`.
The deployed Pi release's 53 code/script files also matched Git repository blobs.
The ordinary `/home/pi/BiomeRTE` checkout was still at the old default branch;
systemd's later `90-ha.conf` overrides selected the pinned release. Checkout HEAD
alone therefore did not identify the running controller version.

The Pi terminal and Docker workers used earlier committed releases. The Mac's
active terminal worker, Docker provider and storage provider matched committed
source. Old OAuth-worker and backup release files were not active services and
are not substituted for the current implementations.

The Mac's active LM Studio coding adapter, HTTP MCP transport, installer and
tests contained changes absent from the branch heads. Those files were recovered
from the installation into `providers/lmstudio`, with the installed dependency
lockfile. Its README was updated to describe the repository layout. Credentials,
machine configuration, databases, caches and third-party installed modules were
not copied into source control.

## Mac controller startup repair

The controller and connector use standard-library Python dependencies. Sampling
the stalled connector found Python blocked in `site` initialization while
listing a package directory, before the connector could start. The qualified
repair adds `-S` immediately after the Python executable in both existing
per-user LaunchAgents. It preserves their paths, identities and environment.

Preview or reproduce that configuration change with:

```sh
python3 -S deploy/repair-macos-controller-startup.py
python3 -S deploy/repair-macos-controller-startup.py --apply
```

The apply operation saves the original plists and replaces the updated files
atomically. It does not restart services. On the repaired Mac these settings
were already applied, and both services were relaunched and qualified.

## Qualification and scope

A controlled Pi-controller outage from 02:17:23 to 02:19:22 UTC on November 4
verified Mac takeover, reads of existing results, a new terminal operation,
journal progress and automatic Pi failback. Both controllers then synchronized.
Pi-local operations did not answer while the Pi controller was down; this is
qualification of the shared site path.

The combined source retains the HA runtime and adds the named-target event
filter required by the LM Studio agent. Regression tests exercise both target
selection and unchanged default ChatGPT routing. The integrated runtime,
gateway, coding-adapter and startup-repair suites are run before publishing.
Source consolidation does not restart the working controllers or change their
private enrollment/configuration. New deployments should use one pinned commit
from `main` and record the effective service command, not just a checkout label.
