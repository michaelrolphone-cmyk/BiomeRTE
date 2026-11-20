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
* A one-time service configuration invokes the stable installed
  `controller_autodeploy_exec.py --config /private/config.json` launcher.
  It resolves `current` once, sets the physical source directory and
  `PYTHONPATH`, and executes the original `controller_argv` unchanged. Preserve
  its interpreter, `-S`, diagnostic wrapper, arguments, environment, user, home,
  logs, and any existing service overrides. Physical paths prevent old
  processes from lazily importing files from a new release during a switch.
* The gateway link initially remains on its independent source path. Optional
  paired deployment is described below. Maintenance, providers and the installed
  watcher remain independent; only explicitly configured services restart.
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
  "controller_argv": ["/absolute/stable/python3", "-S", "-m", "biomerte", "--home", "/absolute/existing/controller-home", "serve"],
  "extra_pythonpaths": [],
  "restart_commands": [
    ["/absolute/stable/python3", "-S", "/absolute/private/controller_autodeploy_service.py", "--config", "/absolute/private/config.json"]
  ],
  "service": {
    "manager": "launchd",
    "label": "org.biomerte.controller",
    "domain": "gui/501",
    "plist": "/absolute/Library/LaunchAgents/org.biomerte.controller.plist",
    "reload_on_restart": false
  },
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
local privilege already configured. The optional standalone
`controller_autodeploy_service.py` implements both: launchd checks whether the
exact configured job is loaded, bootstraps its saved plist if missing, then
kickstarts it. `reload_on_restart: true` first unloads the loaded job and reads
its updated plist; use that during initial launcher adoption, then set it false.
For the Pi configure `service` as
`{"manager":"systemd","unit":"biomerte.service","sudo":true}`. The helper runs
`sudo -n systemctl daemon-reload` and restarts only that unit. Use the actual
Mac UID/domain and plist path; `gui/501` above is an example. The helper never
restarts providers or edits service files. Do not substitute a service installer:
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
tree is accepted as the bootstrap rollback target and whenever that unmarked
bootstrap is used for rollback. This prevents a no-op restart from accepting a
still-running candidate as the legacy fallback. Inspect effective service
settings and the live process independently even when this hook is omitted.

The Linux source probe uses systemd `MainPID` and the physical `/proc/PID/cwd`
link only. It never reads process command-line or environment memory, which can
block behind SD I/O stalls. A legacy Pi service whose working directory differs
from its selected package cannot pass this strict probe before launcher
adoption. Establish its explicit one-time bootstrap proof from separately
captured authoritative service routing, exact PID, and source hash. After
adopting the stable launcher, replace that temporary proof hook with the strict
physical-source probe before enabling branch updates; the launcher guarantees
that the working directory is the selected physical release.

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
4. Copy `controller_autodeploy_exec.py` into the same stable recovery directory.
   Save the exact original controller argument vector as `controller_argv`.
   Change the existing controller's native command to the stable Python,
   `-S`, stable launcher, `--config`, and private config path. The launcher
   executes the saved original command unchanged after pinning the physical
   release. Preserve every unrelated service setting and include any required
   additional Python paths explicitly in `extra_pythonpaths`. On systemd
   inspect the effective drop-in order: a later override can silently defeat
   an earlier command or source-routing change.
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
python3 -S -m unittest tests.test_controller_autodeploy tests.test_controller_autodeploy_service
python3 -S -m unittest tests.test_controller_watchdog tests.test_controller_watchdog_deployment_lock
```

The first suite covers real Git staging with package resources, unchanged
polls, failed deployment followed by a repair, interrupted switches before and
after link activation, offline rollback, failed rollback followed by a new
patch, safe retention, source identity, physical import pinning, diagnostic subprocess timeout, and
authenticated readiness requirements. The Mac watchdog suite separately
checks shared restart-lock deferral. Native service-hook tests mock manager
commands to verify unloaded-service recovery and exact unit scope. Production
qualification still requires
observing the actual services and independently authenticated recovery route.

## Optional paired gateway-link deployment

The same watcher can update the registered gateway link with the controller.
This is opt-in: existing installations keep their original single-service
restart and gateway-readiness checks.

Add `deploy` to `include_paths` before staging the first paired release. The
gateway link imports its entrypoint, dispatch adapters and vendored WebSocket
transport from that directory. Changing `include_paths` does not rebuild a
previously cached immutable commit.

Use a separate private link launch configuration with the same `root` and
`home` as the controller updater. Preserve the link interpreter, `-S`, origin,
key path, home, environment and extra Python paths. Set its `controller_argv`
script argument to the relative path `deploy/gateway-agent.py`. The existing
stable exec helper resolves `current` to a physical release, changes directory
there, and executes that command. This preserves physical import pinning while
allowing a later commit to select a new link source.

A paired configuration uses the existing extension points:

* `include_paths: ["biomerte", "examples", "deploy"]`.
* A `preflight_commands` entry that runs the stable
  `controller_autodeploy_probe.py --config <controller-config>
  --mode link-preflight --release {release}`.
* A second `restart_commands` entry invoking the existing native-service helper
  with the private link configuration. Activation and rollback run the same
  ordered controller and link restart list.
* `linked_services` containing the link's native service descriptor, plus
  `--release {release}` on the existing `--mode gateway` health command.
  The probe checks the running link's physical source before accepting gateway
  registration.
* `gateway.require_network: true` after the compatible gateway is deployed.
  Acceptance then requires a current self-reported snapshot from the selected
  registered controller. An empty successful snapshot is valid; stale, missing
  or unavailable metadata fails the optional gate. The selected link must keep
  the same PID and physical source while its self-report receipt timestamp
  advances beyond the first observed gateway timestamp. Cached metadata from
  the previous process cannot satisfy this check. Both timestamps come from
  the gateway, avoiding dependence on clock agreement between hosts.

The staged link import check starts no link threads, controller instances or
network requests. It imports the entrypoint and local/HA dispatch dependency
closure under the configured Python with site packages disabled.

### Adopt in two phases

A controller-only release usually lacks `deploy/gateway-agent.py`. It cannot be
a rollback destination for a link that has already adopted the shared pointer.
Do not mutate that old release or reset the watcher's durable state to disguise
the missing source.

First preserve the original link plist/unit, updater configuration and stable
probe. At an idle checkpoint with no pending transaction, pause the native
watcher while retaining independent maintenance and the running controller/link.
Install the optional probe and the phase-one updater configuration: include
`deploy` and the link import preflight, but keep the original controller-only
restart and health behavior. Merge the new tested commit, then use the existing
watcher's `--once` mode to stage and verify it while the old pinned link remains
running. Confirm that `current == last_good`, the accepted commit is the intended
new main commit, no transaction is pending, and the immutable release contains
the gateway entrypoint and its dependencies.

Only then adopt the link's stable exec launcher and reload that exact native
service. Verify its physical source, fresh authenticated network registration
and an explicitly addressed controller RPC. Enable the phase-two restart list,
source gate and current-network gate, and resume the existing native watcher.
The next deployment's rollback destination is now the accepted complete release.

If initial link adoption fails, restore the original link service definition
and reload that exact service; retain the accepted controller release and the
phase-one updater configuration. Do not activate paired restart hooks until
the complete baseline is verified. Future paired failures use the normal
watcher transaction to restart both services from its last-good release.

The maintenance agent, providers, gateway credentials and independently
installed watcher program remain outside this paired source switch.

## Frozen-source custody and recovery

A native home with `controller-authority.json` in `frozen-source` mode remains
a physical source identity. Its permitted site health operation is
`cluster.status`, with exact source node, logical service and authority,
`mode: managed-source`, `site_writes: frozen` and
`automatic_failover: false`. It must not execute `system.discovery` to prove
health. Both the watcher and watchdog use this source-aware read path while
retaining their local identity, running-source and external delivery checks.
The source's Application write restrictions are unchanged.

Install `controller_custody.py` beside each stable recovery program: the
watcher, exec launcher and native service helper, and the watchdog's private
copy under the native home's `recovery` directory. The watchdog installer
copies this dependency. These are independent installed programs: merging a
controller release alone does not upgrade the recovery boundary.

Before importing a candidate, accepting a bootstrap, switching a candidate or
rollback pointer, restarting, or executing the selected command, recovery
checks a marked home's source compatibility. The literal
`CUSTODY_PROTOCOL = 1` declarations in Application, Store, and any bundled
gateway entrypoint/event engine are the release's compatibility contract.
Keep that version only while all corresponding custody guards remain. The
stable helper reads declarations using AST without importing candidate code or
opening controller databases. Missing, malformed or incompatible sources are
rejected. A marked managed-authority home is never a native source target.

For a marked home, the launcher accepts a pinned Python command with `-S`
and either `-m biomerte --home /absolute/home serve`, or the selected release's
`deploy/gateway-agent.py` or `gateway/deploy/gateway-agent.py` with the matching
explicit `--home`. Unreviewed wrappers, isolated/import-ignoring interpreter
options, an entrypoint in an older release, or a different home fail closed.
Unmarked installations retain their existing command behavior.

The Mac watchdog also needs an explicit `service_configs` mapping in its
private configuration, pointing to the same stable launcher configurations
used by the real services:

```json
{
  "service_configs": {
    "controller": "/absolute/private/biomerte-autodeploy/config.json",
    "link": "/absolute/private/biomerte-autodeploy/gateway-link.json"
  }
}
```

After custody exists, missing mappings or mismatched source home, service
label, domain, selected release or command prevent the watchdog from issuing
a restart. The exec launcher checks again when launchd or systemd starts a
process, including their automatic restarts. A previously healthy unpatched
LKG is now ineligible: rollback records `rollback_failed`, preserves the
current pointer and pending evidence, and continues to admit a later compatible
repair. It never temporarily points boot recovery at that unsafe LKG.

For first adoption, pause existing recovery actors and stop all unpatched site
and event writers. Hold the existing shared `recovery_lock` across verifying
the independently installed helpers, selecting the compatible physical source,
verifying actual service routing/process source, and installing the source
marker against the reviewed canonical digest. Install the guard on every
actual launch path, including a Pi unit that previously executed a release
directly. Preserve service environments, credentials and independent provider
jobs. The exec child does not acquire its watcher's lock again; that would
deadlock a normal deployment. Only resume recovery after the new native
process proves the exact frozen-source status and rejects native site writes.
Retain stopped legacy releases for evidence; do not select them for a marked
home. Do not remove or relax the custody marker to make recovery green.
