# Controller address registration

Controllers report their assigned network addresses through the existing
authenticated gateway heartbeat. The gateway binds the report to the registered
transport node and its application controller reference. Changing an IP address
does not create another controller identity or transfer work to another owner.

Both the local SQLite link and the legacy HA link include a fresh snapshot on
each heartbeat:

```json
{
  "position": 0,
  "ready": true,
  "load": 0.1,
  "host": "controller.local",
  "network": {
    "version": 1,
    "addresses": [
      {
        "address": "192.168.1.23",
        "family": "ipv4",
        "interface": "eth0",
        "scope": "private",
        "prefix_length": 24,
        "network": "192.168.1.0/24"
      },
      {
        "address": "fe80::1234%eth0",
        "family": "ipv6",
        "interface": "eth0",
        "scope": "link_local",
        "prefix_length": 64,
        "network": "fe80::/64",
        "scope_id": 2
      }
    ]
  }
}
```

The reporting agent does not supply the registration owner or the receipt time.
The existing gateway credential determines the owner, and the gateway timestamps
receipt. Existing controller routing references remain the way to select the
controller; addresses describe its current location.

## Snapshot and freshness rules

A successful report replaces the previous complete address set. An empty
`addresses` list means the current interface inventory contains no eligible
addresses. DHCP changes, removed interfaces and IPv6 changes therefore remove
old entries on the next successful heartbeat.

`network: null` means enumeration is unavailable, including an inventory exceeding
the bounded limit of 128 records. It must not refresh an old self-reported address
timestamp. Omitting `network` identifies a legacy agent and also must not refresh
a prior network report. Last-known addresses may remain useful for diagnosis,
but readers must distinguish them from a current report.

A gateway link remains useful when the controller process fails to start. The
local health request has a three-second timeout, and a failed request produces a
heartbeat with `ready: false` and a fresh network report. Network registration
freshness and controller readiness are independent. An address collection error
does not change an otherwise ready controller into an unavailable one.

## Network interpretation

`biomerte/network_addresses.py` uses the operating system's `getifaddrs` inventory
on Linux and macOS. It has no external dependencies, performs no DNS lookups or
network probes, and uses no third-party public-IP service. It reads enabled
interfaces, excludes loopback interfaces and loopback, unspecified and multicast
addresses, and deduplicates identical interface/address pairs.

Each record retains the interface, IP family and subnet prefix where available.
IPv6 link-local addresses retain an interface zone; that zone belongs to the
reporting host. The scope distinguishes private IPv4/IPv6 networks, public
addresses, link-local addresses, carrier shared space and other special-use
ranges. The gateway validates and normalizes reported addresses.

An assigned public address, a gateway-observed public egress address, and an
explicitly published reachable route are different facts. The interface report
asserts only local assignment. A NAT egress IP does not assert that SSH or any
other inbound service is reachable. Published routes and dispatch ownership keep
their existing registration and authorization semantics.

## Deployment and compatibility

The collector and heartbeat changes remain compatible with Python 3.4 on the Pi
and the Mac's Python runtime. Older gateways ignore the additional heartbeat
field; newer gateways continue accepting older agents that omit it.

Deploy the compatible gateway first, then update the source used by each native
gateway-link service. A separately pinned gateway-link release is not updated
just by advancing the controller runtime's autodeploy pointer. Its launch source
must include `biomerte/network_addresses.py` and the corresponding link modules.

Focused verification covers address changes and removal, private/public IPv4 and
IPv6 classification, interface zones, binary socket layouts, enumeration failure,
oversize reports, health failure and retention of existing authentication:

```sh
python3 -S -m unittest tests.test_controller_addresses tests.test_controller_address_heartbeat
```
