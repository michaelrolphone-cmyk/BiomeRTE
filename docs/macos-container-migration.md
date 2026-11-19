# Mac container migration audit

## Observed environment

The November 19, 2026 audit was performed on the native Mac through the independent maintenance capability. The host is an Apple M2 with 24 GiB RAM. Docker Desktop runs Docker Engine 24.0.7 in a Linux arm64 VM with 8 CPUs and about 7.66 GiB RAM. At the start of this work the VM had about 4.47 GiB free; the Mac filesystem had about 113.8 GiB free. These are separate capacity limits.

The capability registry returned 165 offers across 12 providers plus the built-in provider. An offer is an interface, not necessarily a separate process or a portable workload. Three radio offers were already offline before this migration. The Pi was offline and was not rebooted or physically qualified.

Four containers were already running: biomerte-service-runner, biomerte-relay-state, xiaozhi-esp32-server, and xiaozhi-kokoro. Their presence does not establish that the other native applications have been migrated.

## Placement decisions

| Workload | Container suitability and work | Boundary that must be preserved |
| --- | --- | --- |
| Stream router on port 8751 | First application cutover candidate. Portable entrypoint and image retain the deployed router behavior and use the existing Docker supervisor. No authoritative state volume is required. | Native DeviceRoutes registry and route provisioning stay behind authenticated host I/O. Existing Rover HTTP stream inspection supplies the Mac validator; the public authority-inspection link is not connected. Transfer the native route-refresh loop once during cutover. |
| Rover HTTP service on port 8093 | Portable image/entrypoint preserves control, video, map, survey, and LED APIs. Prepare and test in isolation before stateful admission. | pending-routes.sqlite records uncertain operations. It needs real custody and state transfer; it is not a cache. Keep host identity, native authority authentication, and conservative clock conversion. |
| ToF coordinator on UDP 7446 | Package the actual RiscRTE-System-Apps source closure with NumPy/SciPy and a host I/O client. | Native camera feeds, JPEG data, survey operations, route registry, camera credentials, calibration and pose writes must remain available. A qualified state/effect owner is required before replacing the live coordinator. |
| Private chat and image workers | Network-facing portions are candidates after separating native inference admission from portable workers. | LM Studio/ComfyUI acceleration, ModelRuntime admission.sqlite, active.lock, native process identities and actual inference must stay under the native broker. A Linux PID cannot represent a macOS process. |
| LM Studio coding adapters | Reuse the existing container_runner.py and transition work when extending this migration. | Preserve tokens, agent.sqlite, runs, native inference admission and existing user-facing API behavior. |
| Robot vision and motion providers | Separate portable processing from native capture and host adapters where each input/output contract supports it. | Preserve camera interfaces, Unix-socket feeds, image data, clock domains and calibration ownership. |
| Activation-field and GitLab providers | Further portable-worker candidates after their actual state and provider-registration contracts are packaged. | Private credentials, durable work state and provider identity need explicit bindings and ownership. |
| Workspace workers, terminal, storage and replica providers | Keep the host-facing adapter available; containerize only a defined portable worker role. | Local paths, local disks, process/terminal receipts and host recovery semantics are part of these capabilities. |
| Controller, gateway link, main-branch updater, watchdog, independent maintenance and Docker host adapter | Retain native recovery paths during application migration. | They must be able to recover Docker and deploy a corrective source update. |
| Existing relay authority and Xiaozhi containers | Already containerized; preserve their live state and configuration while adding new services. | Xiaozhi configuration still contains older host-address bindings and needs an explicit future portability review before recreation. |

## Image distribution without a private registry

deploy/docker-services/images.py builds from an exact committed Git export, pins public base-image platform manifests, checks image source labels, runs smoke checks, exports Docker-loadable archives, and writes an immutable release lock with size and SHA-256 receipts. A combined release lock retains all requested platform variants. The existing supervisor accepts immutable local image IDs when images are preloaded.

When a registry becomes available, the same utility publishes to an explicit registry host and namespace, validates every target's provenance before any push, and records confirmed repository digests. Credentials remain in Docker's credential mechanism. This work does not require provisioning a registry.

## Qualification and deployment evidence

Baseline source integration with current main passed 154 native tests with no skips, including service authority takeover, stale survivors, restart, signed fence/activation, expiry, and uncertain Docker custody. Image release regressions exercise source containment, immutable references, archive integrity, platform selection, and publish provenance.

Container-to-Mac connectivity was verified using an isolated authenticated fixture bound only to 127.0.0.1. Docker reached it through host.docker.internal. The production adapter therefore does not need a wildcard listener. It uses private credentials, pins host/boot identity, rejects redirected credential requests, and distinguishes route-refresh ownership from physical effects.

Qualification and cutover must record the actual source commit, image ID, manifest revision, supervisor/grant evidence, port health and external stream reconnection. A built image or an offline standby is not a successful failover receipt. The Pi must acquire its own private bindings, image, state/authority qualifications and live heartbeat before it can be treated as eligible.

Detailed native evidence is retained in the task's maintenance receipts and the docker-migration-20261119-evidence directory: host-inventory-001.json, workload-dependencies-002.json, priority-paths-003.json and coupling-evidence-005.json. Rhizome tracks the migration task and Humus retains the current context and qualification receipts.

## Handoff and recovery

Qualify the host adapter with route refresh disabled. Stage the immutable application image and private configuration, then verify readiness in isolation. During the router handoff, stop the old router's ownership of its port and route-refresh loop, enable the single native refresh owner, and admit the container through the existing coordinator. Rollback must withdraw/stop the container before restoring the native listener and its refresh ownership.

Never share an active native SQLite/WAL database with a Linux container, start a second calibration/control writer, prune unrelated Docker data, or disturb the independent maintenance route to make a candidate appear ready. Registry publication and physical cross-host failover remain separate qualification steps.
