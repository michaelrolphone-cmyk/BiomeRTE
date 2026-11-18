# Portable scoped service authority

This is the first stateful consumer of the generic Docker service runner. The six-channel Wi-Fi relay keeps its existing firmware and provider identity. A fresh logical authority owns only its declared resources, contracts, peer grants, leases, commands and streams. Each controller stores that authority's SQLite state locally.

## Components

- One Docker supervisor per host learns service manifests from the independent Heroku witness and prepares the image for that host's architecture.
- An always-running checkpoint helper on each eligible host receives signed state packages even while its authority child is absent.
- Exactly one admitted authority child serves signed calls for the logical identity. A child starts in preparation mode. Native provider signatures and grants remain mandatory.
- The witness retains transient ownership and checkpoint high-water metadata. Payloads pass through it in flight and remain durable only on the hosts.
- Public provider ingress and new Rover requests resolve declared logical scopes. Declarations contain service and authority identities, resources and providers; they contain no provider-to-host assignment.

The order is durable state restoration, witness acknowledgement of that exact baseline, child startup, real provider fence acknowledgement, witness effect admission, and activation. A healthy process alone never establishes physical authority.

## Images and host configuration

Build deploy/service-authority/Dockerfile for each eligible Docker platform, using an immutable Python base. The qualified base for the actual Pi kernel/Docker version is:

python:3.11-alpine@sha256:7fbad8e4652eae0186429a6f26cf5b23e27e8c6fd7ac4d0cfd7a6988cdeea1e1

Use linux/arm/v7 for this Pi and linux/arm64 for this Mac. Pin the resulting image IDs in the shared service manifest. The runtime uses Python 3.11 and aiohttp 3.14.5 with pure-Python extension fallbacks. It does not depend on the Pi's native Python 3.4.

Each host has a private read-only directory and a durable writable state directory. service.json names the stable worker ID, both replica worker IDs, the logical authority, its exact scope, coordinator origin, key-file paths, SQLite paths, and private adapter endpoint. node.json/node.key belong to the fresh logical service. Copy only this scoped identity and service credentials between the intended replicas; never clone a native controller home.

Run both the helper and supervisor on a user-defined Docker network. The helper has a stable Docker DNS name. Its HTTPS certificate is signed by a private service CA. The authority trusts that CA through adapter_ca_file. The supervisor's SSL_CERT_FILE points to a bundle containing both its normal public roots and the private CA, so coordinator HTTPS validation remains intact. The CA signing key is provisioning material and is excluded from runtime mounts.

The supervisor resolves named mount bindings to each host's own paths. Its adapter entry names the service, HTTPS /adapter URL, private bearer-file path and request budget. The manifest fence_hook uses the same helper's HTTPS /fence endpoint. No credentials belong in the shared manifest, image, logs or command arguments.

The helper runs:
python -m biomerte.service_daemon --role helper --config /config/service.json

The placement child runs:
python -m biomerte.service_daemon --role authority --config /config/service.json

A helper is restarted by Docker unless stopped. Placement children use restart policy no; only the grant-aware supervisor may start them. Both use rotated Docker logs. The helper's child_url_template can use {container_name}, derived from the runner's exact full-binding name.

## Initial native handoff

Initial enrollment is distinct from later automatic host failover. Preserve the provider's actual public key and native grants, and obtain verified source custody before creating a signed bootstrap packet. deploy/service-authority/migrate.py supplies the bounded native-source plan/export checks.

For the relay rollout, first activate declared-scope Rover routing so fresh relay acquisitions require the logical service. Then enable the public provider declaration while its service is unavailable, producing retryable unavailability rather than a fallback to the native issuer. Revoke only the relay provider at the native source, make its six offers offline, and drain the explicitly identified old leases and stream through native operations. Disable admission before releasing reservations so an old queued request cannot become active. Preserve the unrelated Rover peer and native controller services.

The export verifies durable command receipts and the current denied source state. It preserves scoped history and the observed generation floor, and restores the provider/offer permissions only in the destination copy. That source receipt is not a physical fence acknowledgement. The first child still must obtain the relay firmware's real higher-generation acknowledgement before normal calls are admitted.

Install the same signed bootstrap packet on both helpers, start them, then declare the complete service manifest with deploy/docker-services/manage.py. Alternate supervisors learn the admitted manifest automatically. Verify both replicas' exact witnessed checkpoint before testing loss of an owner.

## Ongoing behavior

Every effect-bearing response follows local durable state and a current witness acknowledgement. A lost response is retried with the same native request identity. Unknown physical operations remain uncertain; takeover does not replay them as new commands. The adapter preserves maximum retained generation floors, including unpublished drafts, while selecting the exact witnessed recovery baseline.

An unavailable standby permits the active owner to continue with a locally durable, witnessed high-water mark. An older standby cannot promote until it obtains that exact current state. A witness restart invalidates every old epoch; reestablishing state without its old high-water mark requires all configured replicas. This deliberately does not manufacture freshness when one host might hold newer state.

The helper rechecks the supervisor incarnation independently of incoming RPCs, so restarting a supervisor does not strand a surviving helper on an obsolete connection. The authority's scoped maintenance runs native capability recovery, settlement and expired-stream cleanup under the same serialization and publication rules as calls. It does not run controller workflows or assistant pumps.

A recovered preferred host stays standby while the successor is healthy. Controlled handback is explicit. Physical fence acknowledgement is separate from container health, state readiness and routing readiness.

## Future services

Stateless services need only a pinned image per architecture, a manifest, and explicit health/readiness and stateless effect/state policies. No scheduler code changes are needed. Stateful or physical services require an adapter that can prove their own complete custody, state freshness and effect fence; a writable bind mount alone is insufficient.

The supplied native authority adapter supports declared capacity-one resources with one enforcement provider per resource. Additional resource semantics require their corresponding fence implementation. Enroll public provider or Rover resource scopes only when the service uses those ingress paths.

The Mac's existing Rover/frontend stream bridge and native Router remain separate services. Portable relay authority does not by itself make that entire frontend stack independent of the Mac.

## Validation

Core tests cover checkpoint rollback/conflicts, retained generations, exact preparation/activation bindings, signed native admission and physical fence evidence. tests/test_service_daemon.py exercises real TLS/WS against the companion gateway source selected by GATEWAY_SOURCE, including takeover, lost replies, helper reincarnation, witness restart, stale survivors, trusted-helper TLS, and stream/lease recovery.

The actual Pi/Mac stateless qualification and its retained failed attempts are in docs/docker-service-two-host-qualification-20261118.md and accompanying JSON. That evidence is separate from the later physical relay qualification. Pi undervoltage is an accepted operating baseline throughout deployment and qualification.
