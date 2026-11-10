# Leased capability streaming

Discrete commands retain cap.status as their primary result. command watches default
to 120 seconds with progress renewal; long_command uses output liveness and has no
elapsed-command deadline. persistent resources have only their fence acknowledgement
deadline and explicit configured idle/release policy.

The recovery tick fences and abandons stale generations, then releases capacity.
An abandoned physical effect remains unknown. Neither recovery nor a browser retries
an old physical command. Provider ledgers retain uncertain effects for reconciliation.

## Result consumers and retention

destination.register requires ttl_seconds (1..86400) and an identity-bound inbox.
Invoke with destination only for a registered long-lived consumer. inbox.ack deletes
the entry without enabling redelivery. Terminal actions and capability receipts retain
15 minutes; result inboxes retain their configured duration, bounded by registration
expiry. Capability-path events retain one hour. Terminal output artifacts opt into
15-minute retention; durable knowledge artifacts remain durable.

Per-resource fence watermarks survive guard collection. They are bounded by resource
inventory, rather than one retained row per ephemeral caller.

## Persistent hosting

Run the router on a modern Python host (3.9+), independently of browser connections:

    python -m biomerte.stream_router /private/router.json

Install websockets and, for the Rover HTTP adapter, aiohttp. The router config names
controllers (local controller home directories), certificate, key, listen and port.
Each controller node.json requires a private stream_router_secret of at least 32
characters and stream_gateway_url set to the public wss gateway /api/stream route.
Configure the gateway STREAM_ROUTER service binding or STREAM_ROUTER_ORIGIN to the
router's reachable TLS origin. Deployment must supply this persistent owner; a
stateless Worker or controller-job fallback is deliberately unavailable.

Only stream grants and discrete lifecycle calls touch controller storage. The router
validates the issuing authority and resource fence once per session per second through
the local controller; stream messages never become actions or journal entries.

stream.open requires a bound persistent lease whose offer declares stream.enabled.
The paired server receives its credential; the provider obtains its own role-bound
credential through stream.attach. Credentials expire after two minutes, existing
sessions after the explicit grant TTL. Refresh attachment credentials when reconnecting.
Authority change requires re-fencing and a new session/epoch; never transfer a bare
generation value between controllers.

## Wire and delivery

Attach separate outbound TLS WebSockets for vision, map and control. Each descriptor
URL is suffixed with the granted lane. Authorization is Stream followed by the opaque
attachment credential. JSON messages carry authority, generation, epoch and sequence.
Binary images start with a four-byte network-order JSON-metadata length, then metadata,
then the original image bytes.

Vision is newest-only at processing fanout, provider forwarding, router queues and
browser queues. Frames carry identity, calibration/map revisions, timestamps, pose,
detections, RF and AHRS metadata. The current camera has no synchronized source capture
clock: source timestamp remains unknown and receive/decode time is explicitly separate.
Map deltas carry from_revision and revision. Attachment, overflow or a revision gap
requires a revision-pinned snapshot through the existing map read interface.

## Paired Rover server

biomerte.rover.Rover owns native leases and stream sessions. Start it for the server
lifetime, then mount web_app(rover, authenticate) using the site's existing browser
authentication callback. POST /api/controls accepts mask and fresh_ms, or stop:true.
GET /api/live/vision, /api/live/map and /api/live/control subscribe independently.
Browser attachment/detachment performs no lease or stream lifecycle operation.

Fresh desired state replaces queued state without waiting for applied acknowledgement.
Stop replaces pending intent immediately. Reconnection discards intent and starts a new
epoch, and cannot replay a saved direction. Same-state fresh repeats do not toggle
physical relay contacts. Control is independent of video transport backpressure.

## Relay firmware and qualification

RiscRTE-System-Apps PR 209 adds device.outputs.stream:1 to the existing native provider.
The locally timed output task enforces authority, generation, epoch, monotonic sequence
and a maximum one-second intent lifetime. It rejects motion without a synchronized UTC
clock and applies neutral locally on expiry/disconnect. Received and applied acks are
distinct; neither claims physical contact feedback.

Source and host/CI qualification are separate from deployment. The gateway/router
hosting connection, controller grants/configuration, deployed provider updates, Rover
Site integration and physical timing must be verified on the running infrastructure.
