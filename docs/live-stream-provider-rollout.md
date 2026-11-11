# Live vision and persistent Rover rollout

The paired Mac Rover process owns persistent native camera and relay leases. Browser subscriptions do not acquire or release resources. Preserve existing maps, calibration, camera settings, provider identities and worker ledgers.

## Public interface and session owner

The public interface is https://biomerte-stream-gateway-e0322b2b60c9.herokuapp.com/, from michaelrolphone-cmyk/BiomeRTE-Heorku-Gateway-Stream. The deployment label is not a lease authority: the Mac and Pi controller fingerprints remain authoritative.

Both controllers' private node.json files contain the matching stream_router_secret and publish stream_gateway_url=wss://biomerte-stream-gateway-e0322b2b60c9.herokuapp.com. Secrets are runtime configuration and must not be committed.

The Mac runs org.biomerte.stream-router at ws://127.0.0.1:8751 beside its controller. Heroku controllers.json explicitly selects router_owner: mac_bridge. The authenticated outbound Mac bridge forwards public native sockets to that session owner. Public descriptors and remote provider connections remain WSS. Only an explicitly validated loopback override may use plaintext WS.

The camera provider manifest uses stream_local_router_url; the Rover service uses BIOMERTE_LOCAL_STREAM_ROUTER. Both point to ws://127.0.0.1:8751 and retain the original controller-issued role credentials and fence bindings. Gateway Site v68 retains STREAM_ROUTER_ORIGIN pointing at Heroku for its optional proxy path and authenticated inspection. Direct descriptors avoid the additional Site proxy hops.

org.biomerte.rover-server connects outbound to Heroku. Vision, map, control and each HTTP response have independent connections. Complete snapshots transfer in backpressured 64 KiB chunks, allowing a 10 MB map without placing it on the attachment/control link. No live message becomes a controller action, artifact or durable journal row.

## Readiness, recovery and feedback

Rover Site v22 uses the Heroku origin and a derived server credential with its existing audience. Leases belong to the paired server. A public bridge outage or browser detach must not reacquire them.

The Mac router enables opt-in peer_status. The paired Rover requires the corresponding native capability and reports control ready only when its server socket and the relay provider peer are present. Provider loss clears queued input and readiness; reconnect requires fresh input. Older clients retain the legacy protocol unless this mode is explicitly enabled. The companion installer checks runtime support before stopping the previous service.

Control freshness is separate from transport and lease liveness. ROVER_INTENT_TTL_MS is configured to 500 ms; the router and relay retain the maximum one-second intent bound. Pings, acknowledgements, attachment and lease maintenance cannot extend a driving intent. Reconnection discards saved intent. A fresh desired state supersedes pending state without waiting for an applied acknowledgement.

The private router health body exposes per-authority provider/server lane counts and bounded recent failure categories. Diagnostic files rotate at 1 MiB with one backup. Public Heroku session counts describe that process, not the Mac owner's sessions; inspect private health for native peers. Authority inspection timeouts have been observed and remain a separate recovery investigation from relay ACK delivery.

For opt-in supersedable control, transient authority unavailability pauses forwarding, clears queues and reports notready while retaining valid sockets. Resumption requires exact grant revalidation and fresh input. Definitive authorization/fence rejection still closes the session. Other contracts and legacy protocol retain their existing fail-closed behavior.

## Native vision provider

The vision service exposes owner-only Unix-socket /v1/live and /v1/live/map feeds from the existing decode/CV pipeline. The provider advertises robot.vision.stream:1 as persistent and stream-only when those feeds are available. It publishes newest-only frames. Large map deltas and revision gaps require complete snapshots pinned to map_id and revision.

Keep org.biomerte.robot-vision-provider as the sole worker-lock owner. The qualified idle-stream-1cd797 provider runtime allows an idle HTTP stream body and explicitly aborts its retained descriptor on withdrawal/shutdown. A two-second idle read timeout previously caused reconnects and remote attachment delays that amplified source pauses into 5–9 second public gaps.

The vision service release rover-wire-2aab8017ab6f preserves asynchronous SLAM checkpoint capture/save and cached AHRS/RF telemetry. Wire metadata stays within the 16 KiB header bound, carries measurement-age bindings and explicitly identifies omitted RF scan details. Neither source capture-clock accuracy nor calibrated physical map scale is inferred from transport timing.

## Camera sequence recovery

Camera PR68 (a096730b7f0a3159702ba10bd0284082759fe64e) fixes a sequence reset when a publisher lane is recreated under an existing grant. The provider now reserves local durable ranges of4096 numbers per authority/session/epoch/generation/lane and increments memory between reservations. Its private provider/stream-sequences.sqlite contains binding identifiers, highwaters and fixed grant expiry only; it contains no frame, map or telemetry payload. Expired bindings are pruned. A failed reservation commit prevents publication, and vision freshness is checked again after a reservation that may wait for storage.

The active provider still uses the historical idle-stream-1cd7971d095a runtime path. Only stream_sequences.py, leased_streams.py and biome_provider.py were overlaid atomically with the qualified code; camera-sequence-overlay.json records the actual source and file hashes. Provider PID97168 is the post-restart qualified process. The vision service PID67324, router PID77399 and Rover PID77405 were preserved. Check actual hashes and this manifest before replacing that historically named runtime.

## Recorded qualification, 2026-11-10

These are observations from named qualification windows, not a claim that every deployment gate is closed.

- Final integrated491 runtime qualification340: 456 core tests passed; 450 vision-provider tests passed with26 expected skips. Combined native run exited0 in91.68seconds. Real network tests also cover provider-bound readiness, timeout quarantine, fresh-input recovery and confirmed revocation.
- Vision provider idle-read candidate: 450 reported tests with 26 expected skips. The real Unix HTTP/1.0 regression verifies idle-body continuity and prompt explicit shutdown. The preserved vision service's async checkpoint/telemetry suite reported 449 tests with 26 skips.
- Heroku transport/policy companion: 21 tests passed, including independent lanes and a 10 MB response. Rover Site v22: 160 tests, build and artifact validation passed.
- Two public camera viewers received 429/428 frames over 123 seconds, with 428 matching frame identities and JPEG digests. Maximum processing-to-observer ages were 0.494/0.445 seconds; reattachment took 0.548 seconds. Source capture time remains unknown.
- After the idle-read fix, qualification319 ran 90.775 seconds: local302/public301 frames, 301 shared identities. Maximum local/public gaps were 2.121/2.367 seconds; maximum matched public-added latency was 0.566 seconds. A viewer held without reads for 10 seconds and detached while the fast viewer continued. Source CV pauses remain.
- Qualification326 returned a complete pinned snapshot: revision15524, 6,306 entities, 10,123,213 JSON bytes in7.58 seconds. During that transfer 24 live frames continued, maximum gap0.889 seconds.
- Actual public frames carried valid IMU, attitude, pressure and fresh RF telemetry with18 age bindings; sampled metadata headers were15,787–15,814 bytes.
- Relay firmware PR210 and PR211 were built and flashed to board30:ED:A0:D9:5F:24 with the application at0x50000, preserving Wi-Fi and pairing. Expired attachment recovery and the signed WebSocket handshake buffer were repaired; the final d327 image connected over WebSocket.
- Live neutral controls remain intermittent. Qualification2114 observed both relay peers present throughout, unchanged generation110, no router faults, but only2/8 fresh neutral intents applied to both public subscribers. Separate qualification336 captured a Pi authority-inspection TimeoutError and subsequent peer churn.
- ACK feedback repair PR212 was qualified and flashed. Exact candidatef3a1117ab89c24c3b7de460de31d11a153f68dbd passed native host/pin checks and GitHub42904625604 Linux host/ASan/UBSan plus ESP-IDF6.1 no-PSRAM build. Flash2136 verified board30:ED:A0:D9:5F:24 and wrote only the1,243,632-byte application at0x50000, SHA256c144c5ebeaba6d1751c5d1966e6fee108f3f471bcdf3ce1b5c69679a3e813b9c. Pairing and Wi-Fi partitions were preserved.
- Post-flash qualification2143 observed five consecutive fresh neutral intents applied to both public subscribers, then an authority-check timeout interrupted one intent and the next input was correctly rejected while unavailable; a later fresh intent applied after recovery. Overall6/8, unchanged generation111 and epoch, applied-ACK roundtrips378–1848ms. This is improved feedback evidence, not a complete control-availability pass. The190-second serial window had no ACK write-failure or stale-intent rejection, and one native provider-poll HTTP503.
- Pi timing separated the remaining intermittent failure: direct warm stream.inspect9.93–10.13ms; loopback /health8.67–31.03ms, but authenticated loopback /mcp stream.inspect once exceeded10seconds and later took2641/158ms. Another quieter window returned20/45ms. Resident realm is local SQLite, not a stale remote-journal mode. Public HMAC and signed inspection routes both took1.3–2.9seconds; switching between them was not justified as a speed repair.
- Qualification2312 reproduces the installed publisher defect against the strict router: recreating a lane rejects a fresh frame with Fault409, Old or duplicate sequence. Final candidatea096 qualification2321 passed the complete459-test provider suite with26 expected skips in6.335seconds. Nine new cases cover lane/process recovery, independent lanes, failed durability, expiry and a frame that ages during reservation.
- Qualification2330 after deployment received31 public JPEG frames on camera generation107 and two real map deltas. A complete pinned snapshot at revision15804 contained6,306 entities and10,123,213 JSON bytes; it transferred in8.801seconds while21 fresh frames continued. Maximum processing-to-observer age was465ms.
- Qualification2331 verified an empty private action ledger while holding its writer lock, then restarted only the transient camera provider (95109 to97168). The exact camera sessionc049cbb05a69421182d4734b2a3ebbb3, generation107, epoch01c931dc8db94b5b9369eee1bf327603 and persistent lease were retained. No manual stream.close or lease mutation was issued, and the other stream/service processes were preserved.
- Post-restart qualification2332 received36 public JPEG frames with sequences4260–4295 on that same binding. Both durable lane highwaters advanced4096 to8192. A complete pinned snapshot at revision15812 transferred in7.969seconds with24 concurrent fresh frames. Maximum processing-to-observer age was642ms; map/calibration identities and control generation112 were unchanged, both native publisher lanes were connected, and no new router errors were observed. Map deltas may legitimately be idle; snapshot_required plus a complete pinned snapshot is the recovery contract.
- Short neutral qualification2232 delivered8/8 fresh intents to both public subscribers, retaining its binding and reporting no new authority faults in that window. Subsequent user reports of intermittent physical relay operation keep overall control reliability open; the short successful window is not a sustained availability pass.
- No nonzero motion was issued in these checks. Applied ACK describes commanded output state and does not establish physical contact feedback.

## Open acceptance

Camera publication, pinned map transfer and same-binding provider process recovery are qualified as recorded above. Finish sustained fresh relay acknowledgement qualification and authority-unavailability recovery without replaying old input or weakening definitive revocation. Requalify the final router/Rover/firmware combination after coordinated deployment. Live authority-change behavior and physical contact timing remain distinct checks. A programmatic Site credential does not establish a signed-in browser session; current visual acceptance is unobserved while the connected browser is unavailable.

The active Rhizome task is biomerte-bounded-leases-streaming-20261110. Read its latest coordination comments before any restart or firmware flash. Do not replay uncertain hardware effects or clear unknown outcomes to force readiness.
