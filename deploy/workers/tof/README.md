# Actual ToF worker on the Mac

This deployment runs `tof_coordinator.py` and its real anchor geometry,
front/rear ranging, baseline/refinement, IMU, LiDAR-floor and world-pose
implementation. Its platform is **linux/arm64**. It does not require the Pi
to support the same image.

The image comes from `RiscRTE-System-Apps/deploy/tof`. The image default is
still inert preparation. This direct deployment supplies an **active**
command, so it must follow the owner handoff below.

## Existing interfaces and state

The native host-I/O adapter remains the narrow boundary for device routes,
camera credentials, native vision frames, LAN UDP source identity, and the
Mac monotonic clock. It is not a replacement solver.

The worker uses the existing native directories as bind-mounted state:

- `~/.local/share/biome-tof` -> `/state/tof`
- `~/.local/share/biome-robot-vision` -> `/state/vision`

Native consumers continue reading their existing JSON filenames. The actual
container code and numerical dependencies come from the image; native release
directories are not its Python search path. It uses host-I/O instead of opening
the native route SQLite database or Unix vision socket. Only the worker's
declared JSON inputs/outputs are accessed.

Raw range timestamps are converted back to the Mac monotonic epoch before
publishing `latest.json`. Persistent calibration/world/motion timestamps stay
UTC. Existing survey control is forwarded by host-I/O to the container's
authenticated survey endpoint.

The state directory contains a bounded `request-custody.json` record. The
worker persists a pending front-camera request **before** issuing its HTTP command
and reconciles completion/reboot from device observations. It restores legacy
`latest.json` pending custody on the first migration. Routine sensor uptime
updates do not rewrite this record.

The worker also holds `coordinator.owner.lock` for its process lifetime.
**Docker Desktop and native macOS do not share a reliable flock exclusion
domain. This lock only excludes cooperating container-side solvers.**
The native launch agent must be disabled, booted out, and confirmed exited
before starting the container. This deployment is not a distributed
state-failover protocol.

## Qualify the real image

In the System-Apps worktree, run its committed qualification harness:

```sh
python deploy/tof/ci.py --core-repo /path/to/BiomeRTE --platform linux/arm64 --output /path/to/new/tof-qualification
```

The suite includes a real active coordinator subprocess consuming synthetic
host-I/O UDP/vision data, producing the normal solution JSON, preserving the
native clock boundary and baseline, stopping cleanly and restarting. It also
tests uncertain-request custody and exclusion of a second container-side
solver. No production state or devices participate in that qualification.

## Prepare the concrete local deployment

`prepare.py` inspects a qualified local image and current launch/bridge
configuration. It writes a new private deployment directory with the concrete
Compose file, private client/survey credentials, rollback launch/configuration
copies, and a plan. It does not stop or start anything.

```sh
python deploy/workers/tof/prepare.py --image sha256:QUALIFIED_IMAGE_ID --output /path/to/new/tof-deployment
```

The Compose file uses an immutable local image and does not pull from a
registry. It publishes health and authenticated survey control only on Mac
loopback. The worker runs with the native user's UID/GID, a read-only root
filesystem, and no added Linux capabilities.

Review `plan.json`, source revision, image ID, state paths, camera identity
and the change from `rollback/bridge.json` to `config/bridge.active.json`.
The minimal `config/bridge.patch.json` lists only ToF-owned settings for a
coordinated root rollout. If another worker changes bridge settings before
cutover, merge/rebase against those current settings and prepare a fresh
proposal; the compare-and-swap refuses to overwrite that change.

## Root-controlled cutover

Use the UID, launch labels, plist paths and Compose argv in the prepared plan.
The following order is required:

1. Record current native `latest.json` freshness and consumer visibility.
2. Persistently disable the native ToF launch agent with
   `launchctl disable gui/UID/org.biomerte.tof-coordinator`.
3. Boot that agent out with
   `launchctl bootout gui/UID /path/to/org.biomerte.tof-coordinator.plist`.
   Confirm its exact solver process exited; a submitted stop is not evidence.
4. Run `python deploy/workers/tof/capture.py /path/to/tof-deployment`.
   This refuses a running native solver and captures its final JSON state.
   Vision inputs can still update independently; the receipt explicitly is
   **not** a cross-file atomic checkpoint. State is reused in place.
5. Run `python deploy/workers/tof/bridge_config.py apply /path/to/tof-deployment`.
   This checks native PID absence, persistent launchd disablement and bootout,
   then changes only the reviewed bridge configuration. Restart the existing
   host-I/O launch agent and verify it now owns UDP 7446 and reports active
   anchors/device command access. The bridge change is reversible.
6. Run the prepared Compose command followed by `up -d --no-build`.
7. Run `python deploy/workers/tof/verify.py /path/to/tof-deployment`.
   It requires the exact image, absent native owner, matching state mounts,
   actual `/readyz`, fresh native-reader output and the correct host clock.
8. Confirm the existing robot-vision/provider consumer observes that same
   fresh solution, and verify survey control through its existing interface.
   No new container ownership/ready claim is made before these observations.

No automatic restart of the native solver is permitted while the container
is active. A background native supervisor must not recreate the old owner.

## Rollback

1. Stop the ToF container using the same Compose command plus `stop`.
   Verify its process is no longer running.
2. Run `python deploy/workers/tof/bridge_config.py rollback /path/to/tof-deployment`.
   It refuses a running container and refuses to overwrite later bridge edits.
3. Restart host-I/O with the restored configuration, so it releases UDP 7446
   before the native coordinator starts.
4. Enable the original native launch agent, bootstrap the saved/original
   plist, and verify fresh `latest.json` plus the existing consumer.
5. Keep the current state produced by the container. Do not blindly restore an
   older snapshot and discard newer calibration/world updates. The private
   state capture is available if a specific corruption is established.

Container creation is not a complete migration. Record the verified active
container and native-reader outcome, or record a completed rollback.

## Qualified TLS handoff details (2026-11-20)

Use the existing additional TLS listener with
`--bridge-url https://host.docker.internal:8766 --bridge-ca /absolute/native/ca.pem`.
Preparation copies the CA into the read-only configuration mount and preserves
the combined current HostIO configuration, including native model custody.

On this Mac, launchctl prints enabled/disabled words rather than booleans; the
guard accepts both explicit forms and rejects unknown values. After bootout of
the adapter, wait for its actual PID and launchd registration to disappear before
bootstrap. The adapter drains handlers during shutdown; bootout returning does
not establish completion of that drain. Check model.status has no active request
before initiating the reload. Preserve HTTP8765 compatibility and the existing
host clock identity when qualifying TLS8766.

The real handoff produced a healthy solver container, three physical anchors and
fresh results in the original native-reader state directory. Exact image and
deployment evidence is recorded in ../qualification-20261120.json. Earlier failed
handoff attempts and restored native ownership remain recorded in the private
deployment directory; no older calibration or request ledger was restored over
new state.
