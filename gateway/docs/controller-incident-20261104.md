# Mac controller outage, 2026-11-04

## Confirmed cause

The runtime rejected shared journal transactions larger than 900,000 bytes.
This was an implementation constraint, not a user requirement. Assistant workflow
tokens retained multiple obsolete copies of the same frame. Two runs approached
the threshold; the following step repeatedly failed and rolled back. The Mac's
service log explicitly recorded `Command exceeds shared transaction size. Split
the work.`

The background-loop exception changed site readiness to unhealthy. The connector
then refused all inbox work, including local diagnostics. Its heartbeat remained
live. The Pi was offline, so no other controller could take over.

A second defect left expired or replaced durable delivery leases in a WebSocket's
in-memory inflight map. Those entries could exhaust delivery capacity indefinitely
even after the Mac became ready. Deployment alone did not replace the existing
long-lived connection; reconnecting the Mac restored prompt replies.

## Repairs

- Read-only, owner-authenticated gateway diagnostics operate independently of
  controller readiness. Journal and record reads are paged for transport; paging
  does not truncate the logical record.
- Recovery edits use a shared-head compare-and-swap, preserve journal history,
  and remove only variables proven dead by backwards graph liveness, including
  error branches. Two assistant runs were temporarily recovery-suspended with
  tokens, operation IDs and live frames preserved.
- The Mac runtime removes dead assistant variables during execution and removes
  the 900,000-byte transaction rejection. The corresponding gateway rejection is
  removed. No larger replacement logical transaction ceiling is introduced.
- Background workflow failures are reported separately from shared-store health;
  a successful independent store check retains realm readiness.
- WebSocket delivery capacity follows durable lease ownership. A failed reply
  cannot permanently occupy a connection slot. This does not cancel or time-limit
  the underlying capability operation.

## Physical constraints and remaining architecture

Removing the software ceiling does not remove the hosting provider's physical
request, memory or database row constraints. Current journal entries still occupy
individual database rows. Logical transactions that outgrow physical row storage
need chunked journal storage, rather than another arbitrary logical size limit.
Paging sizes and delivery leases are transport mechanisms, not task lifetime or
CPU limits. Background errors remain visible in `/health` as `background_errors`.

Recovery operations are authenticated through the existing owner MCP route;
they do not add a public administrative endpoint or change Site access.
