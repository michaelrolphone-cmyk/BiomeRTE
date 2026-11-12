# Shared capability streams

Every offer declaring configuration.stream.enabled shares one provider reservation and fence across independent consumer leases. Capacity measures provider reservations, not consumer count. cap.acquire joins the existing reservation for that offer; it does not fence or evict another consumer.

Call stream.open with your own lease. All subscribers receive the same session ID, epoch and endpoint. stream.attach accepts optional lease to select your subscription when one identity owns several. Each attachment credential and socket is independent. Multiple sockets may connect to the exact same session/lane URL simultaneously.

Provider messages are broadcast to all server connections on that lane. Server messages are delivered to provider connections. Input sequences are checked independently per connection; when fan-in requires a common outbound sequence, source_sequence and source_attachment retain original identity. Binary payload bytes are preserved. Slow subscriber failure is isolated.

cap.release releases only that consumer. stream.close optionally accepts lease and closes that subscription; it does not close other consumers. The original provider reservation remains until its last consumer releases. stream.revoke remains an explicit operator-wide revocation. Per-subscription authorization and resource generation fencing remain enforced.

Lane names are declared by the offer; arbitrary application lane names are supported. Sharing is implemented by the capability and stream layers, with no device or capability-name selection.
