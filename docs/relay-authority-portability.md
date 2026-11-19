# Relay authority portability and failover prerequisites

Status: candidate requirements, with live relay qualification blocked. This document does not assert that the relay is containerized or that Pi-to-Mac takeover works.

## Observed system, 2026-11-17

The Waveshare ESP32-S3 Relay-6CH board is itself a BiomeRTE provider. Its firmware registers and receives work over Wi-Fi/HTTPS, and its control lane uses WebSocket streaming. USB is not required for that command protocol; this inspection did not verify the board's physical power wiring.

The native Pi-side responsibility to make portable is the relay's controller authority: provider authentication, resource admission, lease and generation tracking, action custody, and stream grants. The separate authority-inspection link is read-only and cannot replace that authority.

The provider firmware builds a signed envelope with site, realm, id, operation, arguments and auth. It does not include a controller ID. Its configured HTTPS /api/provider/call URL can remain stable while gateway routing resolves the current owner of the provider/resource. Requests must remain signed by the original actor; routing must not substitute an owner credential.

The current resource is relay6ch.30eda0d95f24. Its six contracts share physical capacity one:

- relay.bank:1
- device.outputs.batch:1
- serial.rs485:1
- sound.buzzer:1
- light.rgb:1
- device.outputs.stream:1

The persistent stream has control as its lane, supersedable delivery, and a distinct authority/generation/epoch binding. Command acknowledgment reports a commanded mask; the hardware does not provide relay-contact feedback.

## Live readiness evidence

Read-only inspection of the Mac's current controller found only these trusted peers:

| Peer | Enabled scopes |
| --- | --- |
| 45f172035e6d27e9017cc116f8c32c546d603945942eca6bf39933a04151661b, Paired Rover persistent server | cap.acquire, cap.lease, cap.leases, cap.release, stream.open, stream.attach, stream.list, stream.inspect, stream.close |
| 687eda586198b9e7c2bdad0c6f69731b63552e6a650e6514c663f3d472c8f714, Pi controller | coordination, sync |

Neither peer can publish or poll the relay provider. The actual live provider implementation requires exact offer:<contract> and resource:<resource> scopes in addition to operation permissions. Exact lookups of _enforcers, _capacity and _fence_generations for relay6ch.30eda0d95f24 were absent. No relay offer, guard or current ownership row was observed.

The relay's exact actor fingerprint has not been recovered during this task. No inference about that fingerprint is made from labels or the absence of a text substring. The recipient's missing operation/resource grants are independently established.

The Pi authority was unavailable during the parent's inspection. No authority snapshot, grants or device fence was migrated, and no relay output was issued. Generic Docker runtime qualification is separate from these unmet relay gates.

Evidence operations: audit-relay-trust-20261117-04 and audit-relay-readiness-20261117-05. The latter completed with exit code zero and stdout SHA-256 ef59cfa2824059833282e2b5a7d330f9fe21e0434867ecae5fce11481a1d91b6.

## Service boundary and admission

Treat relay authority as an exclusive, stateful service with a physical enforcement point. Image availability, a running container and an HTTP health response do not establish authority readiness.

Keep the host controller, relay authority, provider firmware, public gateway, stream router and Rover consumer distinct. A failure of one must not silently reassign the identity or state of another.

The initial extraction must either create a dedicated logical relay authority with exclusive custody or use the established controller-to-controller responsibility handoff. Do not start a concurrent clone of the entire Pi controller.

A generic stateless runner is insufficient for this resource until its state recovery and external fencing hooks are implemented and verified. An unimplemented hook must fail admission; a placeholder successful response is not qualification.

## Required state capture from the current authority

Obtain an exact, verified scope through the existing local SQLite and signed handoff mechanisms when the current authority is available. Preserve dependencies and deduplication, without copying unrelated controller responsibilities.

| Required state | Purpose |
| --- | --- |
| Exact relay actor fingerprint, public key and current enabled state | Authenticate the existing board without generating a replacement device identity |
| Exact provider operation, offer and resource grants | Permit only the existing board and six contracts |
| Rover caller identity and consumer grants | Preserve the authorized caller and stream ownership |
| Immutable versioned contract definitions | Avoid changing the meaning of an existing contract version |
| Resource enforcer, exclusive capacity and offer configuration | Preserve one physical board and its admission semantics |
| Guard, holder, generation and retained generation high-water mark | Prevent a replacement authority issuing a stale or conflicting device fence |
| Lease identity, owner, provider, resource and state | Preserve or explicitly supersede the current custody boundary |
| Stream sessions, authority, epoch, generation, lane policy and grant dependencies | Establish a new verified binding without accepting old stream credentials |
| Outstanding actions, definitive results, uncertain outcomes and necessary receipt/tombstone state | Prevent replay of a physical effect whose completion is unknown |
| Responsibility aliases, epoch and signed scope/digest | Bind registry ownership to the exact transferred responsibility |
| Private runtime configuration needed for the selected authority and router | Supply secrets at runtime without embedding them in images or source |

Do not substitute a stale database copy for the latest generation. A readable SQLite file can still be behind the fence durably held by the board.

An interrupted or unknown physical effect remains uncertain after transfer. Missing receipts must not be converted into a claim that nothing ran.

## Ongoing recovery contract

Before enabling unattended failover, define and verify how a prepared alternate obtains usable state:

- The active service keeps its durable state locally.
- Verified checkpoints or signed packages identify source, service generation, resource generation, scope, digest and custody boundary.
- The alternate verifies a checkpoint before reporting recoverability.
- The runtime reports checkpoint lag separately from process health.
- Unknown operation outcomes remain unknown.
- Current-owner coordination uses the existing finite-session/current-state design. Do not restore a permanent shared execution journal.

For a stateful service, image pre-pull alone does not make the alternate ready for takeover.

## Gateway and consumer routing

The firmware's existing provider URL can remain unchanged. The gateway must resolve the current service owner using the stable provider/resource alias, forward the original signed bytes and require the selected authority to verify current trust and scopes.

New consumer acquisitions must resolve that same authority before signing their request. The native Rover bridge currently hardcodes relay6ch-prefixed resources to the Pi. That rule needs an explicit current-owner lookup for new acquisitions as part of relay migration.

Existing operations, leases and sessions retain their established origin and binding. A request explicitly signed for the old controller cannot be rewritten to another controller. A takeover creates an explicit new incarnation and binding; it does not silently move an existing execution.

Unresolved ownership returns unavailable. It must not cause requests to be duplicated across both controllers.

## Successor activation sequence

1. Acquire the generic service's current-owner authority for a new incarnation.
2. Verify and stage the required authority state, exact grants and recovery checkpoint.
3. Withdraw or externally fence the former routing/forwarding authority.
4. Select a resource generation strictly newer than the preserved high-water mark.
5. Deliver the new fence to the relay.
6. Await the relay quiescing its previous physical runner, durably saving the new resource/guard/lease/generation, and acknowledging cap.fenced.
7. Verify that acknowledgement against the exact new binding.
8. Establish new stream grants with the intended authority, generation and epoch.
9. Require both provider and server peers to be present and binding-compatible before reporting control ready.
10. Keep output neutral until a newly issued, unexpired desired intent arrives.

Heartbeat loss is a failure detector, not proof that the old process stopped. Both old-process exclusion and device fencing must be part of the service's activation proof.

The firmware already rejects a lower generation and rejects an equal generation with a different guard or lease. Preserve those checks. Never reset device state to make a stale authority appear current.

## Recovery of a former owner

A recovered Pi starts as a standby. Its old container, leases and credentials cannot reclaim control. It must learn the current owner and generations before participating.

Planned failback uses a controlled handoff. Reconnection or host recovery cannot replay a previously held direction. Fresh input is required after control ownership changes.

## Relay qualification

Qualify these behaviors before reporting relay failover complete:

- Pi authority loss followed by a ready Mac authority.
- Network partition while the old Pi process still runs.
- Old authority commands rejected after a newer fence.
- Delayed fencing acknowledgement and lost activation replies.
- Container, controller and Docker-engine restarts.
- Missing, stale or invalid state checkpoints.
- An unsettled discrete action retained as uncertain.
- Stream recovery without replay of expired desired state.
- Recovery of the Pi without dual ownership.
- Command acknowledgment distinguished from physical motion/contact feedback.

Use neutral desired state for transport and fencing qualification. Physical motion requires its own observed outcome.

## Rover bridge portability constraints

The current native bridge remains running. Replacing it prematurely would break existing functionality.

The inspected active release is /Users/micahelbyrns/.local/share/biome-rover-server/releases/controller-api-20261112. It runs Python 3.11.17 with aiohttp 3.14.5, websockets 15.0.1 and cryptography 50.0.2. Despite the historical PYTHONPATH list, its main biomerte modules and bridge adapters resolve inside that release directory. Preserve the actual resolved source and hashes when preparing an image.

Required interface parity includes:

- GET /api/state.
- POST /api/controls, including stop, survey-zero and LED actions.
- POST /api/led and POST /api/video.
- POST /api/map/snapshot, including streamed world and revision-pinned map snapshots.
- GET /api/live/{vision,map,control}.
- POST /internal/stream/inspect.
- Outbound public bridge, native-router forwarding and independently streamed HTTP responses.

The following assumptions need explicit adaptation before a container cutover:

| Current assumption | Portability requirement |
| --- | --- |
| Controller API at 127.0.0.1:8740 | A configured, authenticated container-reachable authority endpoint |
| Native router at ws://127.0.0.1:8751 | Reachable routing with preserved authentication and transport policy |
| survey_zero.py opens ~/.local/share/biome-robot-vision/survey-pose.sock | A supported network/capability adapter; a macOS host Unix socket is not automatically a Linux-container socket |
| CameraLight defaults to http://192.168.1.45/api/light | The current discovered device route supplied through existing configuration |
| Private material located relative to the host user's home | Explicit runtime configuration and secret paths |
| Relay resources always assigned to Pi | Current-owner lookup for a new acquisition |
| Camera processed_monotonic compared directly to local time.monotonic | A defined clock-domain conversion or portable freshness contract |

The last issue is present in the live FreshRover.broadcast implementation: it assumes producer and server share the Mac monotonic clock and drops a frame unless its computed age is between zero and 0.5 seconds. Docker's Linux VM and the Pi do not share that host clock origin. Packaging without adapting that assumption can silently remove camera frames.

## Source references

- https://github.com/michaelrolphone-cmyk/RiscRTE-System-Apps/blob/e53c5e23dd034b814be589133baee6d8ba380b50/firmware/waveshare-relay6ch/main/BiomeProvider.cpp
- https://github.com/michaelrolphone-cmyk/RiscRTE-System-Apps/blob/e53c5e23dd034b814be589133baee6d8ba380b50/Services/biome_node/ProviderState.h
- https://github.com/michaelrolphone-cmyk/RiscRTE-System-Apps/blob/e53c5e23dd034b814be589133baee6d8ba380b50/firmware/waveshare-relay6ch/main/FreshIntent.h
- https://github.com/michaelrolphone-cmyk/RiscRTE-System-Apps/blob/e53c5e23dd034b814be589133baee6d8ba380b50/profiles/waveshare-esp32-s3-relay-6ch/biomerte-capabilities.json
- https://github.com/michaelrolphone-cmyk/BiomeRTE/blob/c2b41435494ff55735d48f403598416989bd7c5b/biomerte/ownership.py
- https://github.com/michaelrolphone-cmyk/BiomeRTE-Heorku-Gateway-Stream/blob/12231f21de9425f2074aa3bcd66aff24267fbbf4/mac/rover_service.py

Repository references explain protocol behavior; the runtime-specific findings above came from the named live audit operations. Repository main is not a claim about the currently deployed source.
