# Controller restoration and automatic recovery — 2026-11-05

The hosted gateway tried to decode a controller-native discovery page as a full legacy snapshot (`snapshot.capabilities`). This raised an exception and returned HTTP503 while ordinary `system.discovery` and terminal execution still worked. Gateway version29 now forwards default/filtered discovery and the legacy alias to the controller's `system.discovery`, preserving its continuation and realm. Native and OAuth route regression tests cover the actual response shape. Old snapshot cursors return a restart-discovery error instead of an infrastructure failure.

A second failure path existed in `ControllerLink.next`: an open WebSocket suppressed the durable HTTP inbox forever, even when socket delivery stopped. Empty sockets now fall back to that same durable inbox every2 seconds. HTTP and socket delivery keep the same job IDs, lease ownership and durable controller receipts; this does not create a new command or replay uncertain provider effects.

## Independent supervision

`deploy/controller_watchdog.py` runs under its own macOS LaunchAgent, `org.biomerte.controller-watchdog`. It does not depend on controller workflow scheduling or the capability queue. Each pass reads:

1. Local authenticated `system.info` in the local realm.
2. Authenticated `system.discovery` in the shared realm.
3. The gateway's authenticated journal/cluster status, independently of the connector.
4. The existing authenticated `/bridge/check` round trip, through the durable connector inbox.

Three consecutive failing request-path probes restart only the existing controller or connector service. A reachable local control path plus an unavailable gateway/network remains visibly degraded without pointless host restarts. A healthy restart result alone never means recovered; successful probes must follow. Failed restarts are recorded and retried with increasing delay. Unloaded services remain unloaded.

The watchdog does not terminate terminal/model/provider jobs, rewrite journals, change fences, cancel commands, release occupied resources, or relabel uncertain effects. Maintenance command execution retains its separate Android signature requirement. Fixed service supervision does not expose an arbitrary-command backdoor.

## Configuration and observability

On the Mac, configuration and status are below `~/.local/share/biomerte-controller/recovery/`. `config.json` contains endpoint and key-file paths, not copied secrets. Credentials are reread for rotation. `status.json` atomically records last observations, exact failure categories, restart counters, cooldowns and actions. Logs never include keys, request headers or response error bodies.

Defaults: 15 seconds between probe passes;15 seconds per network probe;3 failed passes;60-second initial restart cooldown, doubling to300 seconds while failure persists. These are configurable diagnostic and restart scheduling intervals, not job duration/CPU limits. There is no retry-count cutoff. Add a `paused` file or set `enabled:false` for deliberate maintenance; remove it to resume. Independent launchd KeepAlive restarts the watchdog itself.

## Qualification

-44 hosted gateway tests passed; deployed version29; normal filtered discovery and `discovery.page` both verified against the running Mac.
-9 recovery tests passed on Linux and native Darwin arm64. A disposable server kept `/health` green while MCP requests hung; supervision recovered it while an independent subprocess completed normally.
-Tests cover an open-but-idle socket, preserved inbox identities, transient failures, repeated connector failure, restart cooldown persistence, upstream outage without restart, fixed restart targets, credential rotation and atomic status.
-Live Mac pre-install probes all passed: local, site, upstream and delivery.

The Pi is offline. This change qualifies the active Mac path; it does not claim a physical Pi rollout or exhaustive reliability under all failures. Deterministic application defects and unreachable upstream services remain failures requiring their own repair; restart is not treated as proof of repair.
