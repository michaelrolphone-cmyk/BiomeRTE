# Independent controller automatic deployment

The Pi and Mac controller can follow the repository's `main` branch. A small
standalone watcher fetches a changed branch head, stages that immutable commit,
checks the package, switches a source symlink, restarts the existing controller
service, and verifies the running process and authenticated request paths.

A controller startup failure does not stop this watcher. It restores the last
verified release, rejects that failed commit, and keeps watching for the next
patch merged to the configured branch.

## Components and ownership

* Install `deploy/controller_autodeploy.py` as a private copy outside all
  controller releases. Run it using the host's stable Python interpreter with
  `-S`; it supports Python 3.4 and later and imports only the standard library.
* Its configuration and `status.json` are local files, separate from controller
  state. It never creates a controller identity, calls `init`, edits `node.json`,
  changes credentials, or modifies service definitions.
* A one-time service configuration points the existing controller's source
  directory and `PYTHONPATH` at the watcher's `current` symlink. Preserve its
  interpreter, `-S`, diagnostic wrapper, arguments, environment, user, home,
  logs, and any existing service overrides.
* The gateway link, maintenance agent, providers, and installed watcher remain
  on their existing independent source paths. This watcher only restarts the
  explicitly configured services.
* The installed watcher does not replace itself from a newly fetched release.
  Updates to the recovery mechanism require their own verified installation.

## Configuration

Use a mode-0600 JSON file, with absolute paths appropriate to that host:

```json
{
  "root": "/absolute/private/biomerte-autodeploy",
  "home": "/absolute/existing/controller-home",
  "repository": "https://github.com/michaelrolphone-cmyk/BiomeRTE.git",
  "branch": "main",
  "python": "/absolute/stable/python3",
  "interval": 60,
  "restart_commands": [
    ["/absolute/restart-helper"]
  ],
  "recovery_lock": "/absolute/private/biomerte-autodeploy/recovery.lock",
  "health_command": [
    "/absolute/stable/python3", "-S", "/absolute/private/gateway-readiness.py",
    "{commit}"
  ],
  "bootstrap_source_command": [
    "/absolute/stable/python3", "-S", "/absolute/private/verify-current-source.py",
    "{release}"
  ]
}
```

`restart_commands` is a nonempty array of argument vectors. Commands are executed
directly, without a shell. For the Mac the normal controller restart is
`/bin/launchctl kickstart -k gui/<actual-uid>/org.biomerte.controller`. On the Pi
it is the existing service manager's controller restart, with the required
local privilege already configured. Do not substitute a service installer:
the historical installer may initialize identities or replace working service
settings.

`health_command` is optional in the program, but configure it when external
gateway delivery is part of deployment acceptance. It must make a real
authenticated read against the intended controller and exit nonzero until
ready. It runs after the built-in authenticated loopback checks. Keep
credentials in existing private files; do not put tokens into command
arguments. `{root}`, `{home}`, `{release}`, and `{commit}` are literal argument
placeholders.

`bootstrap_source_command` is an optional read-only check that the existing
process really uses the supplied initial source tree. It runs before that
tree is accepted as the bootstrap rollback target. Inspect effective service
settings and the live process independently even when this hook is omitted.

Other settings:

| Field | Default | Meaning |
| --- | --- | --- |
| `include_paths` | `["biomerte", "examples"]` | Paths archived from the exact commit. Preserve package JSON/static resources and `examples/capability-followup.bpmn`, which the continuation loader reads. |
| `local_origin` | `http://127.0.0.1:<node-port>` | Controller loopback origin. Non-loopback origins are rejected to protect the local admin token. |
| `git_timeout` | 60 seconds | Deadline for one Git subprocess. |
| `command_timeout` | 60 seconds | Deadline for one restart command. |
| `preflight_timeout` | 120 seconds | Deadline for controller imports and each optional preflight command. |
| `startup_timeout` | 120 seconds | Readiness observation window before rollback. |
| `probe_timeout` | 10 seconds | Deadline for one authenticated probe or external health hook. |
| `probe_interval` | 2 seconds | Delay between startup readiness observations. |
| `retry_seconds` | 60 seconds | Delay before retrying a transient staging failure. A newer branch head bypasses the delay. |
| `preflight_commands` | none | Additional argument vectors run in the staged source directory. |
| `enabled` | true | Pause new deployments. An interrupted deployment is still reconciled first. |

These deadlines apply to the updater's own Git, probes, preflight, and restart
commands. They impose no limits on terminal jobs or controller capabilities.

## Initial installation

1. Verify an independently authenticated maintenance path and preserve the
   existing service files and exact working source. Reconcile deployed
   contributor fixes into the branch before switching to it.
2. Copy the watcher into the stable private recovery directory. Configure its
   root and all command paths. Verify Python and Git access under the actual
   service account, without interactive prompts.
3. Run the installed copy with
   `python3 -S /stable/controller_autodeploy.py --config /private/config.json --initialize /existing/verified/source`.
   This validates the existing node and authenticated local/site paths,
   optionally verifies process source, and creates `current` pointing to that
   original source. It does not restart any service.
4. Change only the existing controller's source routing to use `current`.
   Preserve every unrelated service setting. On systemd inspect the effective
   drop-in order: a later override can silently defeat an earlier
   `WorkingDirectory` or `PYTHONPATH` change.
5. Configure the existing Mac watchdog's JSON `recovery_lock` to the same path.
   Its shared-lock support defers a competing restart while the updater is
   switching or verifying. A process crash automatically releases `flock`;
   no persistent pause flag can strand recovery.
6. Run an observed one-shot update using `--once`. Verify its status, the
   controller's exact `system.info.release.commit`, and external authenticated
   delivery before enabling unattended operation.
7. Install an independent OS-supervised service using the examples below and
   verify it is running. Keep the stable installed watcher outside the source
   symlink.

A boot-persistent Pi systemd unit can use the existing controller account:

```ini
[Unit]
Description=BiomeRTE independent branch deployment
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
ExecStart=/usr/bin/python3 -S /stable/controller_autodeploy.py --config /private/config.json
Restart=always
RestartSec=10
StandardOutput=null
StandardError=journal

[Install]
WantedBy=multi-user.target
```

There is intentionally no `Requires=`, `BindsTo=`, or `PartOf=` relationship with
the controller. Stopping or failing the controller must not stop its updater.

On the Mac use a separate launchd label, for example
`org.biomerte.controller-autodeploy`, with this argument vector:

```text
/absolute/stable/python3
-S
/stable/controller_autodeploy.py
--config
/private/config.json
```

Set `RunAtLoad=true`, `KeepAlive=true`, and `ThrottleInterval=10`. Choose the
already supported launchd user/system domain for this host. A LaunchAgent starts
when that user's launchd session is available; machine-boot operation before
login requires a correctly installed LaunchDaemon with the intended user.
Do not rewrite the controller or maintenance labels to install this service.

## Update, failure, and interruption behavior

The watcher first reconciles any unfinished source switch using its durable
transaction. It attempts rollback before making a network request. If the
previous release cannot be restarted, it records `rollback_failed` and retains
the pending transaction while still accepting a newer repair commit. A failure
while staging that repair cannot erase the earlier rollback destination.

An unchanged branch uses `git ls-remote` and performs no fetch, release rewrite,
status rewrite, or service restart. Only a changed commit creates a temporary
depth-one Git repository. After archiving and staging the required source, the
temporary repository is removed, so historical Git objects cannot accumulate.

Preflight imports the actual controller dependency closure using the configured
host Python with `-S -B`. It does not instantiate Application or initialize
controller state. Optional rover/async modules that require a newer interpreter
are retained in the package but are not compiled as a gate for the Python 3.4
controller. Use `preflight_commands` for additional real entrypoint checks.
The source tree and marker are synced before an atomic rename, followed by an
atomic `current` symlink switch. Activation state is synced before the switch.

New release acceptance requires:

* The original node and site identity through an authenticated local
  `system.info` call.
* The exact staged commit in `system.info.release.commit`. `biomerte/release.py`
  captures this at process import from the immutable release marker, using
  the resolved source path. Moving `current` cannot make an old process claim
  the new commit.
* A working authenticated site discovery request.
* Success from the configured external `health_command`, when present.

A bad preflight never changes the running source. Failed startup or readiness
restores the previous verified source and restarts the same controller service.
The rejected commit is not retried every poll. A new branch commit is evaluated
normally; transient fetch failures are retried after their configured delay.

Retention removes only directories inside this watcher's `releases` with its
own matching marker. It preserves the original bootstrap, current release,
last good release, previous good release, and the most recently rejected
release when applicable. It never deletes arbitrary old deployment trees,
controller data, provider files, or another tool's directories.

## Status and tests

Read status without mutation:

```text
python3 -S /stable/controller_autodeploy.py --config /private/config.json --status
```

`status.json` records the desired, attempted, current, last-good, and rejected
commit; pending recovery; deployment errors; and Git polling errors. It is
written only on transitions. `changed_at` is a state-change time, not a
heartbeat. The OS service state establishes whether the watcher process is
running. Repeated identical Git failures preserve the same `poll_error`
without repeatedly writing the SD card; successful polling clears it.

Run the isolated recovery tests from a checkout:

```text
python3 -S -m unittest tests.test_controller_autodeploy
python3 -S -m unittest tests.test_controller_watchdog tests.test_controller_watchdog_deployment_lock
```

The first suite covers real Git staging with package resources, unchanged
polls, failed deployment followed by a repair, interrupted switches before and
after link activation, offline rollback, failed rollback followed by a new
patch, safe retention, source identity, diagnostic subprocess timeout, and
authenticated readiness requirements. The Mac watchdog suite separately
checks shared restart-lock deferral. Production qualification still requires
observing the actual services and independently authenticated recovery route.
