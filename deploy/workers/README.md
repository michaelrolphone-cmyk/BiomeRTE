# Actual application worker containers

This directory packages the application implementations running on the Mac and Pi.
Each family has its own image, persistent-state contract and deployment instructions.
The inventory covers application workers, their actual execution code, native
dependencies and the state needed for a supervised handoff.

## Implementation coverage

| Family | Code inside the image | Qualified platform | Required external/native dependency |
| --- | --- | --- | --- |
| GitLab worker | Actual Git operations, repository catalog and durable worker | Linux ARM64 | Existing GitLab server; native credential custody |
| Workspace worker and monitor | Workspace lifecycle, checkpoint/disposal and Docker-backed workspaces | Linux ARM64 | Mac Docker daemon; preserved workspace paths |
| Docker CLI worker | Command execution, output artifact spool and durable operation replay | Linux ARM64 | Existing Mac Docker daemon |
| Storage worker | Workspace allocation, reserve checks and storage capability operations | Linux ARM64 | Native volume mounting and capacity/identity attestation |
| Replica worker | Existing archive/replication implementation | Linux ARM64 | Existing remote replica backend and archive state |
| Private chat worker | Signed sessions, encryption, request ledger, provider polling and response delivery | Linux ARM64 | Native LM Studio and native model admission |
| Private image worker | Encrypted image sessions, progress, artifact chunking and request custody | Linux ARM64 | Native ComfyUI/Metal and native model admission |
| ToF coordinator | Numerical solver, anchor/range state, calibration, camera request custody and survey control | Linux ARM64 | Registered physical sensors; native LAN UDP and clock adapter |
| Motion observer and provider | Actual observation recording, motion model, planning and training | Linux ARM64 | Registered rover streams and native vision input |
| Vision provider | Actual capability dispatch, video jobs and front/rear/map transport | Linux ARM64 | Existing native vision compute process |
| Rover application | Paired application, authenticated HTTP/WebSocket transport and durable route state | Linux ARM64 | Existing route authority, TLS ingress and host transport |
| Coding tools | Actual MCP/HTTP application, filesystem tools and durable job client | Linux ARM64 | Existing workspace/GitLab/Docker capability workers |
| SDR | Actual RTL/USB/serial acquisition, NumPy processing, durable daemon and socket protocol | Linux ARMv7 | Qualified Pi USB/serial devices |
| FM | Actual compiled C++ transmitter backend, durable daemon and socket protocol | Linux ARMv7, matching Pi 3 hardware | GPIO/DMA/device addresses and original radio hardware |

The image identities and qualification receipts are recorded in
[qualification-20261120.json](qualification-20261120.json). Family instructions are
in [general](general/README.md), [private](private/README.md),
[tof](tof/README.md), [motion](motion/README.md), [vision](vision/README.md),
[radio](radio/README.md), [rover](rover/README.md) and [coding](coding/README.md).

A successful image build is distinct from a live service handoff. The qualification
record preserves that distinction, including failed attempts and native restoration.

## Native execution requirements

Architecture restrictions are explicit. Existing MLX/Metal implementations
(AF, FieldTransform, ContextReconstruction, LM Studio inference and the current
ComfyUI/vision MPS engines) depend on Darwin APIs or native GPU execution.
Their Linux worker containers preserve those native dependencies. An ARM64 image
does not supply a Darwin kernel or Metal.

The complete Linux CPU vision recipe is present, with the actual SLAM/detector
sources and deployed native patches. Its first build exhausted Docker VM storage
before engine compilation. It has **not produced a qualified CPU image**.
The follow-up recipe reduces unused checkout data and package caches, but that
revised build, real Linux inference and performance remain unqualified. See
[the CPU receipt](vision/qualification-cpu.json).

Host terminal execution, maintenance/recovery, controller recovery, native model
admission, volume mounting, Docker bootstrap and physical host I/O retain their
host responsibilities. Existing Xiaozhi and Kokoro containers remain existing
deployments. No registry is provisioned by these changes.

## State and failover

Images contain code and dependencies. Existing provider/site/resource identities,
signing keys, phone pairing, operation ledgers, workspace roots, learned motion
state, ToF calibration and archive state remain outside images.

Current Mac handoffs reuse state on the same Mac. That is process isolation and
restart recovery; it does not by itself establish cross-host state replication or
safe active/active operation. A second host needs the required hardware/backend,
the correct state, identity and secrets, and a single authoritative owner.

The native kernel and Docker Desktop VM do not enforce one shared file lock.
The qualified handoff therefore disables the original launchd job, verifies its
processes exited, checks current custody, captures stopped-state evidence and only
then starts the pinned replacement. The general handoff freezes resolved Compose
configuration and verifies the exact container image, service and execution user.
Rollback checks custody before and after stopping the replacement, and preserves
current ledgers rather than overwriting them with older snapshots.

Private chat/image workers move together. Their durable signing and pairing state
is preserved, while ephemeral handshake/session keys remain RAM-only by the
existing protocol. In-progress exchanges must drain; subsequent exchanges create
fresh sessions. Native model execution retains FIFO admission, cleanup and process
custody even if a container connection drops. Model calls do not retry implicitly.

## Native boundary

The existing HostIO adapter keeps HTTP port 8765 for current clock/route clients
and adds TLS port 8766 in the same process. New private model, gateway credential
and GitLab-token traffic requires TLS, the existing bearer credential and the
registered host identity. Native model module paths are SHA-256 pinned. Model
payloads and session keys are not written into adapter logs or persistent state.

Native VideoJobs uses the actual installed vision interpreter and module directory,
independently of the route helper. Host clock translation preserves native sensor
timestamps across the VM boundary. Native storage attestation supplies the actual
mounted-volume identity, reserve and capacity rather than the VM bind mount's
different filesystem view.

A supervised HostIO deployment must set both PYTHONPATH and WorkingDirectory to
the new immutable release; an old working directory takes precedence during
Python module loading. Keep the previous configuration and launchd definition,
qualify the candidate, compare configuration hashes before replacement, drain
active model calls, and verify both HTTP and TLS listeners after startup.

## Build and publication

Build recipes and source hashes are checked in. The Mac images are qualified for
Linux ARM64; Pi radio images were built for Linux ARMv7 and the existing older
Docker API. No claim is made for untested architectures or physical radio output.
The FM fixture never transmitted RF.

Images use immutable local IDs in prepared deployments. A registry can later host
the same images without changing application state or provider identity. Publishing
an image or selecting a new version is separate from this source conversion;
automatic updates must retain the same qualification and ownership gates.

## Deployment status on 2026-11-20

The qualification catalog records fourteen built image families. The ToF solver
has transferred to its container and is processing physical anchor input using
the original state and clock. Its route-metadata timeout crash was repaired in
System Apps commit e79ce5ed2a8a; the final image passed 91 tests and a live
observation of more than 45 seconds with zero restarts, fresh output and unchanged
native clock identity. /healthz returns 200. /readyz still returns 503 for the
pending Rover camera dependency, so Docker correctly retains an unhealthy
readiness status. This is a running, degraded solver, not full camera readiness.
See [the live qualification record](tof/live-qualification-20261120.json).

Docker CLI, storage and the private chat/image pair returned to their original
native ownership after guarded replacement attempts failed fresh gateway polling.
Both native and container requests encounter the site authority's frozen-native-
write error. Current ledgers, signing identity and pairing were preserved; native
ownership restoration does not establish healthy provider polling.

GitLab and workspace transitions are gated by existing uncertain operations.
Motion and vision transitions are gated by stale coherent vision observations
and unavailable Rover control. Rover and coding have concrete local bundles that
preserve their existing loopback endpoints; Rover still requires a quiescent route
state transfer. Replica has explicit credentials, current-IP and native-volume
attestation wiring, but the current Pi endpoint timed out during SSH banner
exchange before host-key verification or authentication. No archive transfer was
attempted. Radio images are built and fixture-tested; physical-device handoff is
pending. See each family's qualification/preparation record for exact evidence.
