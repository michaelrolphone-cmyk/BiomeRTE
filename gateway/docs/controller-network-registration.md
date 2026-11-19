# Controller network registration

Controller network observations belong to the existing authenticated
`controller_nodes` registration. The gateway derives the node from its bridge
credential and resolves its application reference through the existing
`CONTROLLER_NODE_MAP`. Network reports cannot choose an owner, replace a route,
transfer a lease, or grant access.

Each local/HA link heartbeat may include:

```json
{
  "network": {
    "version": 1,
    "addresses": [
      {
        "address": "192.168.1.55",
        "family": "ipv4",
        "interface": "en1",
        "scope": "private",
        "prefix_length": 24,
        "network": "192.168.1.0/24"
      }
    ]
  }
}
```

A successful report replaces the complete address snapshot, including an empty
list. It contains at most 128 addresses. Interface names and IPv6 link-local
zones identify the local network on which an address is meaningful; different
controllers can legitimately report the same private address. The gateway
normalizes the IP and derives its scope and subnet independently. Wildcard,
loopback, multicast and limited-broadcast addresses are not accepted as host locators.
Private, public, link-local, shared address space, and special-use observations
remain distinct. Assigned addresses do not assert that SSH or another service is
reachable.

The server records its own receipt time. A fresh controller heartbeat does not
refresh an older address report. Omission supports old clients; `network:null`
marks enumeration unavailable immediately, preserving the last known snapshot
with its original timestamp. A later successful report restores current data.
Network freshness depends on the heartbeat interval and enabled registration,
independently of controller application readiness. An unhealthy application can
therefore still report a current IP.

The gateway separately records Cloudflare's `CF-Connecting-IP` as an observed
egress address. Arbitrary forwarded-IP headers are not used. This may identify a
NAT or proxy and is not an assertion of inbound reachability. The cross-zone
Worker sentinel and requests with a CF-Worker marker are retained as proxy
observations with no controller address; ordinary observations still carry
unverified egress attribution because upstream Workers can affect this header.
Its own timestamp
expires independently. The public route remains the existing authenticated
native `/v1/call` endpoint with the application `controller` selector in the
signed body; no new resolver, address authority, or routing registry is created.

Authenticated HA status, `controller.diagnostics`, and the controller discovery
catalog return `network.self_report`, `network.gateway_observed`, and
`network.route`. Each observation exposes its timestamp and freshness; clients
must check `current` before using an address as current. Last known addresses
remain available for diagnosis after expiry. Private IP metadata is not added
to anonymous public responses.

The schema upgrade is additive and idempotent: `controllerNetworkSchema` adds a
bounded JSON metadata column to `controller_nodes` once. Concurrent Workers
recheck after a competing ALTER; existing clients and an older Worker can still
use the registration. Address metadata and heartbeat state are committed in one
UPDATE, and an older overlapping heartbeat cannot replace a newer observation.

The existing provider topology registration also refreshes `host`,
`addresses`, and `updated` when a caller supplies fresh `provider_context`,
even if its controller attachment stays unchanged. Reusing retained context
for legacy calls does not refresh an unchanged attachment; an actual attachment
change still updates its epoch and placement time. Owner, epoch, and pinned
dispatch routes keep their existing semantics.

Scope normalization follows the IANA IPv4 and IPv6 special-purpose registries
(last updated 2025-10-09), including more-specific public exceptions.
Transition addresses without a general global-reachability assertion remain
special; scope never proves an installed service or an inbound route.

- https://www.iana.org/assignments/iana-ipv4-special-registry/
- https://www.iana.org/assignments/iana-ipv6-special-registry/

Provider locator refresh also checks server receipt order, so an older
overlapping same-owner observation cannot replace newer registration metadata.

## Device delivery checks

The existing authenticated `POST /bridge/check` route resolves a registered
transport to its unique application controller reference in `CONTROLLER_NODE_MAP`
and sends `biome_status` to that owner. A device therefore verifies its own
round trip even when the gateway's default controller is offline. The request
body cannot select another owner or operation. Missing or ambiguous mappings
fail before queueing; controller errors remain errors. Unregistered legacy
bridge credentials retain their existing default-controller behavior. This
keeps the watchdog's real delivery probe and restart policy intact.
