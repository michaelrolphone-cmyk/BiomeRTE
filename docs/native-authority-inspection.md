# Native lease-authority inspection

The persistent router owns sessions on the Mac. Heroku provides public TLS and WebSocket ingress. Each controller remains the authority for its own leases; the Pi remains the relay authority.

The Pi runs `deploy/authority_link.py` as `biomerte-authority-link.service`. It establishes an outbound WSS connection to `/authority/{controller_fingerprint}/connect`. This connection is independent of the Mac Rover bridge, video, map transfers and the controller command queue.

The router calls the private HTTPS `/stream/inspect` endpoint. Each request contains authority, session and a current integer timestamp, with a body HMAC in the existing Router authorization header. The gateway sends a fresh correlated request to the registered controller. The agent accepts only inspection messages for its own controller and invokes `stream.inspect` on the authenticated loopback native API. The native HTTP reply is `{"result": <grant>}`; malformed or ambiguous responses fail closed.

Every reply must match its originating socket, authority and session. The Mac router then compares the complete lease/generation/epoch/site/realm/provider/server binding before forwarding. No grant cache supplies authority evidence. Confirmed rejection remains distinct from temporary unavailability: 403/404/409 stay definitive; disconnects and timeouts return unavailable. Pending requests and reply sizes are bounded. No commands, frames, maps or telemetry are stored by this transport.

## Configuration and service

The agent reads the existing private controller `node.json`: `node`, `admin_token`, `port`, `stream_router_secret` and a root WSS `stream_gateway_url`. Secrets are never passed on the process command line. Python 3.4 and later are supported with the repository's vendored websocket client.

Render `deploy/biomerte-authority-link.service.in` with the actual Python executable, release, vendor, controller home and service account. On the Jessie Pi, run enable and start separately; its systemctl does not support `enable --now`. The service reconnects automatically and remains independent of controller restarts.

Shutdown first interrupts the socket at the kernel, then uses the websocket client's immediate shutdown. It must not enter a WebSocket close/receive handshake from a signal handler while the receiver holds its read lock.

## Qualified deployment

- Core source df8e828: all 487 native core tests pass. The seven inspector tests also pass on the Pi's actual Python 3.4.
- Heroku PR2 merge 2dbe887 is deployed. All 26 gateway tests pass, including independent coexistence, authentication, fresh reads, wrong peer/binding, disconnect/reconnect and unavailable behavior.
- Native Pi inspection after connection reuse: 80 successful reads, median 18.3 ms, maximum 128.2 ms.
- The old queued Site inspection hop took 4.4 seconds and timed out. The actual Mac–Heroku–Pi path passed 30 fresh reads: median 434 ms, maximum 597 ms.
- An absent inspector returns 503. Restart recovers the exact same generation113 session, epoch and lease. The corrected service stopped in 0.196 seconds; stop timeout settings were not shortened.
- The Mac router now uses Heroku for Pi authority reads. Its controlled restart preserved the controller, Rover and camera-provider processes. Both control peers and all vision/map peers reconnected, with no quarantine or router errors.
- Post-cutover camera delivery and a complete pinned 10 MB map transfer passed while fresh frames continued. Existing map and calibration identities were retained.

Whole-spec acceptance remains open while final relay reliability and the independent gateway command-owner routing defect are tracked. Do not infer physical relay contact feedback: the installed board has no contact sensors.
