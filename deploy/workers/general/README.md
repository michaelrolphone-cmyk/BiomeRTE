# General implementation workers: native ownership handoff

The images in this directory run the actual GitLab, workspace, Docker CLI,
storage and replica implementations. `handoff.py` performs a same-Mac ownership
transfer while preserving their existing identities, paths and current ledgers.

## Prepare a concrete plan

Use a new private output directory for each plan:

```sh
python3 deploy/workers/general/handoff.py prepare COMPONENT --version QUALIFIED_IMAGE_TAG_VERSION --client-dir EXISTING_HOST_IO_CLIENT_DIRECTORY --output NEW_PLAN_DIRECTORY
```

Preparation reads the existing native launchd definitions, original loaded and
disabled states, image identity and configured bind sources. It resolves Compose
into a per-component JSON file, pins the immutable image ID, removes builds,
disables pulling and sets every bind to require an existing source. Environment,
Compose and applicable ownership-proof files are hashed. Subsequent commands
reject changed prepared files and changed native definitions. Use a new plan
when changing configuration; do not edit an existing prepared plan.

Docker CLI and both workspace processes use `0:0` only with an accepted native
bind-owner fixture. The default receipt is
`~/.local/share/biomerte-worker-containers/native-bind-owner-proof.json`;
`--docker-bind-proof` can supply its exact alternative location. It must use
schema `biomerte.native-bind-owner-proof/1`, record container user `0:0`,
mode `0o600`, successful readback and matching observed/expected native UID/GID.
The receipt is copied into the frozen plan. Storage, replica and GitLab preserve
the native account UID/GID. A supplementary group alone is not treated as proof
of Docker Desktop socket access.

Storage mounts only the existing terminal identity/node.json and terminal-manifest.json metadata; the native identity directory and private keys are not exposed. GitLab, workspace-worker and workspace-monitor explicitly mount the configured Git workspace_root at the identical absolute path, including when it is outside their state directories. Preparation requires that source to exist.

The existing HTTPS HostIO configuration, CA and token remain readonly mounts.
No HostIO production port is compiled into this script. For capability workers,
the bridge and selected controller must answer before any native owner is
stopped. Replica checks bridge availability but does not require a capability
controller for its independent SSH replication.

## Replica endpoint and native storage checks

Replica preparation additionally requires `--replica-host CURRENT_PI_IP`. Obtain
that current address from the existing controller's gateway self-report. The
frozen environment supplies `BIOME_REPLICA_HOST`; the source volume UUID still
comes from the installed `replica.json`. This leaves that native configuration
unchanged and does not silently fall back to its old address. A later address
change requires a new prepared plan.

The replica service mounts the supplied HostIO client directory read-only,
including its CA and token, and the existing workspace submount read-only at
its original absolute path. Before taking the replica lock or changing backup
state, the container uses the existing native `storage.status` observation and
volume identity/reservation checks. It never formats or mounts native media.

The existing storage state is writable for the archive, health and staging
files. The exact existing `replica-key` and `known_hosts` files are overlaid
read-only. SSH retains user `pi`, host-key alias `biome-sacred-pi`, BatchMode,
IdentitiesOnly and StrictHostKeyChecking. Global host-key lookup and automatic
host-key updates are disabled; no new key is accepted.

A read-only SSH qualification uses the existing key/known-hosts options with
`-N` and no remote command. It must reach public-key authentication before a
source is considered reachable for replication. That check does not run the
remote `snapshot` command, transfer backup data or prove a complete backup.
The offline tests and container volume attestation likewise do not start sync.

## Apply and recover

```sh
python3 deploy/workers/general/handoff.py apply NEW_PLAN_DIRECTORY/plan.json
python3 deploy/workers/general/handoff.py rollback NEW_PLAN_DIRECTORY/plan.json
```

These are operational commands. Run them only during a coordinated host rollout;
the source tests never invoke a real handoff.

Application creates stopped containers first and verifies image, service,
execution-user and container identities. A pre-existing running replacement is
a blocker. It records native transition intent before each launchd command,
disables and removes the original jobs, waits for their processes to exit, checks
custody again, and starts the exact recorded container IDs. It never recreates
a replacement from a mutable tag during handoff.

Custody checks include worker actions, workspace `active_operation` and
checkpoint/disposal/provisioning journals, and incomplete replica `.sync-*`
directories. Normal active but idle workspaces are allowed. Unresolved work must
be reconciled by its existing operation identity; changing owners is not a
substitute for that reconciliation.

Rollback checks custody before stopping replacement containers and again after
they stop. It refuses an unexpected/replaced container owner. It then restores
the original native loaded/enabled state and exact original plist/source paths.
An unloaded or disabled job is not accidentally activated. It does not overwrite
current ledgers with an older backup. Ledger snapshots are recovery evidence.

If a command fails after native transition begins, the plan records its failed
phase and error. Automatic restoration is attempted only when custody is readable
and settled. A failed identity, integrity or custody check blocks restoration
and records the reason. Read the plan's status/transitions before retrying. A
native per-component lock serializes handoff commands on the Mac; that lock is
not used to infer exclusion between Mac processes and Docker VM processes.

## Readiness evidence

Ordinary workers require a successful poll marker created in container-local
`/run/biome-health` tmpfs after that container's `StartedAt` time. The workspace
monitor retains its existing process check; the associated worker must provide
the fresh polling evidence.

Replica requires its container-local marker written by the existing entrypoint
after `backup.sync()` returns successfully. It does not inherit success from
native `replica-health.json`. Its health allowance is 12 minutes, covering the
configured ten-minute startup period and health-check intervals. An unfinished
transfer is retained for reconciliation if readiness cannot be established.

## Focused verification

```sh
python3 -B -m unittest discover -s deploy/workers/general -p test_handoff.py -v
```

The tests use temporary state and mocked host commands only. They cover immutable
configuration/image enforcement, controller-offline ordering, partial native
transition recovery, launchd state preservation, uncertain operation refusal,
post-stop races, workspace/replica journals, administrative-user proof and fresh
replica readiness. They do not build images, stop services or call live providers.
