# Live vision and persistent Rover rollout

The paired Mac Rover process owns persistent native leases. Browsers subscribe to its vision, map and control lanes; attaching or detaching a browser does not acquire or release resources. Camera and relay grants remain separate. Existing state, maps, calibration, camera settings and provider identities must be preserved.

## Deployed transport

The public session router is https://biomerte-stream-gateway-e0322b2b60c9.herokuapp.com, from michaelrolphone-cmyk/BiomeRTE-Heorku-Gateway-Stream. Its deployment label is not a controller authority: the actual Mac/Pi fingerprints remain the authorities. The gateway exposes public WSS descriptors and authenticated stream inspection. Inspection validates the current owning controller's grant; a signed attachment alone is insufficient evidence of current authority.

Gateway Site v68 uses STREAM_ROUTER_ORIGIN and a private signing secret. Both controllers publish wss://biomerte-controller.cantikash.chatgpt.site/api/stream descriptors. Secret values belong only in private runtime configuration.

org.biomerte.rover-server connects outbound from the Mac to the Heroku Rover ingress. Each live lane and HTTP response has a separate connection. HTTP responses transfer in backpressured 64 KiB chunks so a complete map can exceed a single message size without occupying the attachment/control link. No controller action, artifact or durable journal row is created per live message.

Rover Site v22 uses the Heroku origin and a derived server credential. Its existing custom audience is preserved. Native control readiness is reported separately from service reachability. Complete revision-pinned snapshots have no short state-request timer. Source: Site commit7df1057046cd8bf6abdd42fadf4a3b756bfb2fb1, Heroku commitb21d451d881360f4d27765d570363fa8f4c3ca30.

The optional Mac loopback router, org.biomerte.stream-router at127.0.0.1:8751, remains installed. Its private-tunnel adapter is an alternate deployment path, not a prerequisite for the deployed Heroku ingress.

## Native vision provider

The service exposes owner-only Unix-socket /v1/live and /v1/live/map feeds from the existing decode/CV pipeline. The provider advertises robot.vision.stream:1 as persistent and stream-only only when those feeds are available. Large map deltas become pinned snapshot requests before exceeding the metadata bound.

Install requirements-streaming.txt in the existing vision virtual environment. Keep the canonical org.biomerte.robot-vision-provider as the sole worker-lock owner. Preserve its saved worker ledger and identity. The provider implementation may remain on the preserved vision release while provider-manifest.json repo selects the integrated worker runtime; verify compatibility before changing that runtime. On2026-11-10, the provider implementation was byte-identical between streaming-e2840545e298 and full-spec-7f791edf85e7, while worker.py differed. The manifest was updated to the integrated worker and the canonical provider restarted.

The Rover owner refreshes lane attachment credentials under the same lease/epoch. A failed sender clears control readiness and pending intent even if its receiver remains alive. Late map subscribers receive the latest known map reference; map queue overflow retains a pinned snapshot target. Owner replacement invalidates the cached map reference.

## Qualification recorded on2026-11-10

- Current pre-PR60 native runtime:445tests passed in92.2s. PR60 sender/map regressions:12focusedRover tests passed after integration.
- Vision provider suite:437tests total,26skipped in this native environment; test skips are not live qualification.
- Heroku:14tests passed, including actual independent WebSocket lanes and a10MB chunked snapshot. Rover Site:160tests, build and artifact validation passed.
- Public camera reverse ingress delivered20matchingJPEGs to two subscribers. Processing-to-subscriber age:median285ms,max634ms. Source capture-clock accuracy remains unknown.
- Public pinned map snapshot revision15157 for mapb1b584da-30d3-4935-9b10-f5bcad4c0c38 returned complete:true,6,283entities,10,082,852JSONbytes in7.67s. No pages were dropped.
- Legacy discrete relay ownership was neutralized to mask0 and released. Applied output is distinct from physical contact feedback. No motion was issued in these checks.

Open acceptance is explicit: sustained public ownership has shown repeated revocations and is being requalified; relay persistent fencing remains blocked by expired stream-attachment recovery in firmware; neutral streamed acknowledgements, physical timing and live authority-change behavior still require qualification. A Sites dispatch credential returned401SIGN_IN_REQUIRED from the Rover API and does not establish authenticated browser acceptance. Existing public camera/map successes do not close these gates. Current GitHub qualification is queued, not passed.

The relay correction is tracked in RiscRTE-System-Apps PR210. Do not replay uncertain hardware commands or clear unknown external effects to force readiness. The active Rhizome task is biomerte-bounded-leases-streaming-20261110.
