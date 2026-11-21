# Actual Rover application container

This image runs the installed Rover browser/API application: its signed controller calls, durable route ownership, control and vision streams, map snapshots, video-import routes, LED adapter and survey-zero adapter. It uses the captured application closure in `deploy/portable-services/source` and the already adapted `rover.py` entrypoint. It does not start the generic service runner, a controller, or a separate stream-router process.

The native application currently comes from `service-authorities-0b5d03ea8465/rover`. `source-provenance.json` records every image source path and hash, the original deployed source hashes, and the explicit transport/clock adaptations. No native settings, keys or job databases are in the image.

## Build and isolated qualification

Build from the repository root:

```sh
docker build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" -f deploy/workers/rover/Dockerfile -t biomerte-worker-rover:"$(git rev-parse --short=12 HEAD)" .
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,noexec,size=32m,mode=1777 --entrypoint python IMAGE_ID -B -m unittest discover -s /app/tests -p 'test_*.py' -v
```

The default image command prints help. It does not start the application. Tests use fixture authorities and local loopback only; they never acquire a live capability or move a device. They exercise real HTTP authentication, browser routes, binary vision streaming, signed controller addressing, durable uncertain-operation ownership, original stream expiry/epoch checks and the cross-host vision clock fence.

## Runtime contracts and native dependencies

Copy `rover.example.json` into a private configuration directory and resolve its values from the existing enrolled installation. Bind that directory read-only at `/config`. The example is not a deployment configuration. Preserve the existing Rover identity, controller destination, site, registered resource IDs, gateway URLs, channel assignments and private router origin. Mount only the required node metadata and signing key inside this directory. Bind the supplied HostIO client directory separately at /run/biome, read-only; the example uses /run/biome/host-io.json. Its HTTPS URL, registered host ID, ca_file and token_file come from the host rollout, including the local CA. No HostIO port is compiled into this image.

The application uses the current controller API and private stream router over the configured network. The existing authenticated native HostIO owner remains responsible for the registered source clock, current device-origin resolution and survey estimator reset. Model/vision/motion implementations and physical relay/camera devices remain separate dependencies. No Docker socket, device node, host process namespace or broad home-directory mount is needed.

The container configuration explicitly sets `listen_host: 0.0.0.0`; the default remains `127.0.0.1`. Compose publishes container port 8093 only on host `127.0.0.1:8093`, preserving the existing native motion observer endpoint. The application retains its existing outbound authenticated gateway transport. Browser paths remain `/api/state`, `/api/controls`, `/api/led`, `/api/video`, `/api/map/snapshot` and `/api/live/{lane}`; their external ingress remains the existing gateway. The existing browser/API bearer authentication still applies to the published loopback endpoint. The internal stream-inspection route retains its existing authentication.

## Durable handoff and rollback

The current native application stores `pending-routes.sqlite` beside its enrolled identity. This database owns uncertain operation routes. Do not start a fresh empty database while prior operations are unresolved, and do not share a writable SQLite database across simultaneous native/container owners.

Before a real cutover, quiesce the native Rover application and settle or explicitly retain its pending operation custody. Transfer `pending-routes.sqlite` with SQLite's backup API to a dedicated state directory while the old owner is stopped; include a backup of the original database and its exact source identity. Bind only the dedicated state directory read-write at `/state`. The container UID must be able to write it. The configuration and identity remain read-only. The Compose defaults match the observed Mac account convention; set UID/GID explicitly for the target host.

Set `BIOME_ROVER_IMAGE` to the qualified immutable image digest, `BIOME_ROVER_CONFIG`, `BIOME_ROVER_STATE`, `BIOME_HOST_IO_CLIENT` and `BIOME_DOCKER_NETWORK`, then use the Compose file only as part of the coordinated host rollout. Run the image's `--check-config` first with the prepared mounts. Confirm the current controller and HostIO clock destination are reachable before accepting live traffic. The application requires `state_mode: single_host_durable` and refuses generic stateless failover activation.

For rollback, stop the container, preserve and transfer its latest route database back to the native owner's directory using SQLite backup, restore the original launch configuration and then start the original application. Reconcile pending operations against their recorded authority; never redirect uncertain submissions merely because a new placement is available.

This package enables a restartable dedicated application container. Cross-host automatic failover of its recovery state and physical device/control behavior still require a qualified handoff; the image qualification does not assert those outcomes. Nothing in this package has installed, stopped or started a live launchd job.
