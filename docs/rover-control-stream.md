# Rover control stream wire protocol

Discover device.outputs.stream:1, acquire your own shared subscription, wait for a bound lease, and call stream.open. Connect to the returned URL with /control appended and Authorization: Stream <attachment_credential>. Keep the subscription alive using its advertised heartbeat. Release only your subscription.

Send JSON text, or binary with a four-byte unsigned big-endian JSON metadata length, that JSON, then optional bytes. Switching text to binary does not change the metadata schema.

Every control frame requires type="desired", authority, generation and epoch copied from stream.open, an increasing integer sequence per connection, outputs={"mask":N}, and issued_at/expires_at as UTC Unix seconds. expires_at must be greater than router UTC now, no later than issued_at+1, and issued_at must not be in the future. For a 400ms intent compute expires_at=issued_at+0.400 immediately before sending. Account for network/clock uncertainty; never replay expired movement intent.

Channel bits: 1 forward (mask1), 2 back (mask2), 3 left (mask4), 4 right (mask8). Combine throttle and steering with bitwise OR; mask0 stops all relays. Never combine forward/back or left/right. Example neutral frame:
{"type":"desired","authority":"<open.authority>","generation":"<open.generation integer>","epoch":"<open.epoch>","sequence":1,"outputs":{"mask":0},"issued_at":"<current UTC seconds number>","expires_at":"<issued_at+0.400 number>"}
Placeholders are bindings with the indicated numeric types, not literal strings.

A direction/duration pulse object alone is not this wire protocol. Relays are binary, without PWM. The speed-versus-hold-time curve remains uncalibrated. Expiry stops intent; ping, attachment and credentials do not prolong movement.

Validation failures reply {"type":"error","code":"STREAM_REJECTED","status":400,"reason":"...","lane":"control","applied":false} and close the offending attachment with1008 (policy),1007 (invalid JSON) or1009 (oversize). Unexpected transport failures use1011. Other subscriptions remain intact. A rejected frame is not forwarded. Provider acknowledgments describe commanded state, not contact feedback or measured physical movement.
