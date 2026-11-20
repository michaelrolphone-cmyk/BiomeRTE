# Actual SDR and FM worker images

These images contain the functional radio implementations, including the SDR FFT/capture/receiver-routing code and the FM transmitter, audio preparation, native backend, watchdog and physical clock-cleanup code. They also carry the BiomeRTE provider client needed to expose the existing contracts.

## Source and platform

The imported sources were read from the Pi's active systemd ExecStart paths on 2026-11-20. `installed-source.json` records original paths and SHA-256 values. The original FM backend binary was identified and hashed, but is not committed: the image rebuilds its captured C++ source using the captured makefile. The backend's BSD license is retained in source and the image.

Both recipes target **linux/arm/v7**, matching the current Pi. They use the immutable Debian Bookworm ARMv7 manifest in `build-inputs.json`. Debian packages provide the ARMv7 Python, NumPy, pyserial, librtlsdr and libusb implementations, avoiding an assumption that current PyPI ARMv7 wheels exist.

The FM adapter intentionally retains its exact Raspberry Pi 3 Model B Rev 1.2 and peripheral-address checks. It cannot run the GPIO4 transmitter on the Mac. The SDR worker requires its actual physical USB/serial receivers on the execution host.

### Changes from the imported workers

- Both daemons can signal readiness through an inherited pipe, so the provider starts only after that daemon has bound its real socket.
- FM can read an image-generated backend path/hash manifest. Without it the native original backend path and exact hash remain the defaults. The image manifest pins its freshly compiled backend.
- One FM fixture normalizes a macOS temporary-directory symlink; runtime behavior is unchanged.
- The container launcher reuses the enrolled provider home and rewrites only each offer's adapter argv into an ephemeral manifest. It does not pair, initialize identities, change contracts/resources, or overwrite the native manifest.
- Shutdown leaves the FM watchdog time to finish its existing clock cleanup before the container init exits.

## Build and qualify

From the repository root, using an ARMv7-capable builder:

```sh
docker build --platform linux/arm/v7 -f deploy/workers/radio/Dockerfile.sdr -t biomerte-worker-sdr:SOURCE .
docker build --platform linux/arm/v7 -f deploy/workers/radio/Dockerfile.fm -t biomerte-worker-fm:SOURCE .
python3 deploy/workers/radio/qualify.py --kind sdr --image sha256:QUALIFIED_IMAGE_ID
python3 deploy/workers/radio/qualify.py --kind fm --image sha256:QUALIFIED_IMAGE_ID
```

The qualification commands use no network, hardware, host state or credentials. They import the actual dependencies, run the existing worker tests (13 SDR and 9 FM tests), test container boundaries, and launch the real daemon on isolated temporary state to verify status, durable replay and conflicting-operation rejection. They also exercise the provider CLI. This is functional image qualification; it does not establish physical capture/transmit behavior or old-kernel runtime compatibility.

The default image command is `check`. Running the actual enrolled worker requires explicit `serve --home ... --state ...` arguments and the existing state mounts.

## Pi deployment

`launch.py` is compatible with the Pi's Python 3.4 and emits Docker CLI arguments supported by Docker API 1.30. It does not stop native services, pull images, enroll providers or infer device paths. Fill an example config with the qualified immutable image ID and observed receiver nodes/GIDs.

`plan` prints the exact command without changing the host. `check-host` verifies host/image identity, required state/device paths, absence of native ownership and absence of unsettled FM jobs. `run` repeats those checks and executes Docker.

The provider home and worker state keep their original host paths and ownership (pi UID/GID 1000). They are writable bind mounts so receipts, receiver selection and monitor settings survive replacement and rollback. The private provider home is never included in the image.

### Device and privilege mapping

- SDR maps only explicitly listed receiver USB and tty nodes, adds their host GIDs, drops all capabilities and sets no-new-privileges. The receiver code retains its stable identity and antenna-routing rules.
- FM maps `/dev/mem`, the host device tree read-only, and the exact shared native exclusion-lock file. It adds SYS_RAWIO plus SETUID/SETGID/DAC_OVERRIDE/AUDIT_WRITE for the existing sudo watchdog transition. Its sudoers rule permits only the protected watchdog script and its state-directory job arguments; the image is read-only. No privileged-container flag is used.
- FM audio input needs an explicit read-only subdirectory mount under `/home/pi`; the home directory is not broadly mounted.
- Pi host networking preserves the current provider endpoint. These Pi rollout definitions are not Mac Docker Desktop definitions.

The existing shared FM lock must already be a regular file. If the native worker has never created it, prepare that one root-owned lock file before a planned handoff; the package never broadens this into a mount of all host locks or devices.

### Explicit transition and rollback

A rollout owner must first inspect current SDR scans and FM jobs. Reconcile unsettled operations; do not replay them. Export the built image and load it on the Pi before changing service ownership. Preserve the existing provider and worker state and both native unit definitions.

For each selected worker:

1. Stage the qualified image, reviewed config, this launcher and the corresponding supplied systemd unit. Adjust the unit's `/opt/biomerte` source location to the staged checkout if needed.
2. Stop its native provider and daemon as one planned handoff. Verify they and any hardware watchdog have settled. Do not run old and new owners concurrently.
3. Run `launch.py check-host`, then start the container unit. Its daemon keeps the same existing service lock. Check registration and result/status paths before any physical operation.
4. If qualification fails, stop the container unit and confirm the process/watchdog has settled. Restart the two original native units against the same provider and worker state. Their manifest is unchanged.

The supplied units declare conflicts with the corresponding native units. No unit has been installed or started by this package. No physical SDR capture or FM transmission is part of image qualification.
