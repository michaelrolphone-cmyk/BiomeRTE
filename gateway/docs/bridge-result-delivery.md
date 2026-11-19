# Full-result bridge delivery

The legacy `/bridge/complete` endpoint accepts at most 500,000 request bytes.
An observed 548,554-byte unfiltered discovery MCP result cannot pass that HTTP
fallback. The WebSocket path does not use this parser; without its exception
logs, a WebSocket failure or a D1 failure is not established by that observation.

Large results now use authenticated HTTP completion frames, independent of the
socket. Each ASCII JSON fragment is at most 65,536 bytes. This is a transport
frame size, not a cap on the number of discovery contracts, offers, or bytes in
the complete result. No fields are removed and no pagination is exposed to the
legacy caller. Each D1 value is bounded; reconstruction queries read 16 frames
at a time, avoiding a single unbounded D1 row or query result.

Chunks are keyed by the original durable job ID, lease, SHA-256 digest and part.
Writes require the job's controller ownership and current claimed lease. Identical
retries are accepted; conflicting chunks are rejected. Commit checks contiguous
parts, the digest and the MCP content array before atomically marking the original
job done with a small internal manifest. All bridge result reads reconstruct the
original MCP result. Foreign-key cascade removes chunks with expired bridge jobs.
Small results retain the existing socket-first, HTTP-fallback completion path.

## Rollout and rollback

1. Apply `0002_bridge_result_chunks` (included in the migration journal).
2. Publish the gateway source with chunk endpoints and manifest-aware reads.
   Existing agents and inline completion rows remain compatible.
3. Update controller source, including the new `deploy/result_delivery.py`, then
   restart the authorized controller link service. Both HA and legacy agent paths
   must use the matching source. Python 3.4 syntax and standard-library APIs are
   preserved by the production module.
4. Verify full unfiltered discovery end to end, including HTTP fallback, compare
   contract/offer counts and the full result, then verify small calls and durable
   retry results. Local tests are not proof of hosted delivery.

Do not deploy new agents before the gateway supports the new endpoints. For a
rollback, stop new chunk-producing agents first and retain manifest-aware gateway
reads until all existing manifest jobs have expired or completed their retention
period (currently one hour). Keep the table during that interval. Dropping it or
rolling gateway readers back immediately would lose access to completed results.

No cloud publication, migration, credentials, service restarts or gateway settings
changes are performed by this source patch.

## Progress and read resumption

Accepted chunks renew the current unexpired claim for 35 seconds and advance its
queue eligibility and cleanup timestamp. Commit verification renews between each
bounded database read. There is no total upload duration deadline. An expired or
replaced lease cannot be revived by a stale uploader; after a stalled transfer,
a caller retry makes the existing job eligible for a new controller claim.

`cap.discover` now uses the normal durable request ID path: retries with the same
request ID and identical arguments retrieve the same discovery snapshot, including
when the initial approximately 20-second HTTP wait finishes before the upload.
Use a new request ID for fresh discovery. Reusing an ID with different arguments
is rejected. `cap.status`, `cap.submission`, leases and worker inbox reads retain
their existing fresh-read behavior even when their request ID is reused.

The one-hour bridge retention still applies. A transfer that stops making progress
for the lease period must be reclaimed; this is an inactivity lease, not a total
operation deadline. Cloud request duration, memory and subrequest quotas still
apply to final full-result reconstruction and delivery. This patch removes the
500,000-byte frame failure and prevents slow results becoming unreachable by a
same-request discovery retry; it does not promise unlimited hosted result sizes.
