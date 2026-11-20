# Checkpoint helpers for multiple services on one worker

A supervisor identifies a physical worker and its current incarnation. Each
checkpoint helper owns exactly one service store. Relay and controller helpers
therefore register the same physical worker identity with separate service_id
values. The companion gateway indexes the exact (worker_id, service_id) pair
and correlates each request and reply with that connection, incarnation,
coordinator epoch and grant binding.

New helpers wait for a matching scoped registration acknowledgement before
reporting readiness. Both remote checkpoint operations and private local
adapter bindings reject another service before accessing the store or chunks.
A replica RPC may carry an owner's grant from the other physical worker; its
service must still match the receiving helper. This preserves replication
between hosts without opening another service's store.

## Existing relay compatibility and deployment order

Deploy the companion's scoped transport first. Its bounded registration
procedure recognizes the currently deployed relay helper without changing its
image, private configuration or durable state. The legacy helper sends no
service_id, but already handles checkpoint.status before the registration ACK.
The gateway obtains a read-only, exactly correlated, authenticated durable
current checkpoint reference and binds that socket once to the reference's
configured service. An empty, mismatched or unavailable legacy proof is refused.
There is no default service, wildcard connection, or fallback by worker alone.

The new helper requires service_id in its registration ACK, so it remains
unready against an old gateway. This makes partial deployment visible instead
of routing controller operations to an existing relay store.

Connection registration does not establish a witness head or grant authority.
Initial head enrollment still requires every configured replica's authenticated
durable checkpoint evidence and complete replication acknowledgement. Existing
select_current and publication policies are unchanged.

## Qualification

tests/test_service_helper_scope.py checks remote and local rejection before
state access, acknowledgement correlation, and readiness timing.
The companion tests exercise real concurrent WebSocket connections, stale and
cross-service replies, duplicates, timeout, reconnection and exact enumeration.

The cross-repository TLS case in tests/test_service_daemon.py runs the captured
deployed legacy loop, RPC handler and enrollment methods alongside current
controller helpers on both same worker IDs. It verifies independent get/status,
all-replica first enrollment, missing-service refusal, controller block
replication, and unchanged relay high water. The fixture records the deployed
source hashes and contains no live credentials or state.

Run with GATEWAY_SOURCE pointing to the companion commit under qualification.
The fixture and generated local keys/stores are disposable; no live helper,
coordinator, runner or authority is needed.
