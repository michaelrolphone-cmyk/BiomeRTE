# Transient-session and bounded-streaming integration

This candidate combines BiomeRTE bounded leases, destinations and streaming at
df898e2c7549e76672361af2ea2d3eb383175b84 with the source in the deployed Mac release
session-api-db3ce72c61f12665, inspected on 2026-11-10.

It preserves local coordination sessions, controller addressing, ownership
fences, receipt streams, operational retention, concurrent worker dispatch and
read-only SQLite access. Both indexed-query interfaces remain supported.
Lifecycle reads include dirty-index fallbacks without writing through a read-only
connection. Artifact uploads preserve the existing positional transient argument
and add the optional TTL by name. Provider ingress retains controller binding
while accepting the new liveness and streaming operations.

Rover startup polls its existing lease. Failure and cancellation cleanup close
the stream and release pending or bound leases. Browser lifetimes remain
independent of resource ownership.

Qualification is recorded in the PR and retained host logs:
- /tmp/bounded-transient-final-tests.log
- /tmp/bounded-transient-gateway-tests.log
- tests/test_bounded_session_integration.py covers both index APIs and dirty
  records under a read-only transaction.

This source is not a production cutover. The inspected Mac node had neither
stream_router_secret nor stream_gateway_url, and the public gateway environment
had no STREAM_ROUTER_ORIGIN entry. The inspected processes included live vision
but no persistent router or Rover server. Reachable persistent hosting,
controller grants, Rover Site integration, provider/firmware updates, and physical
frame/control timing remain required. Do not restore a permanent shared journal
or use durable per-message jobs as a streaming fallback.
