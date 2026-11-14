# Complete rover telemetry
Rover telemetry has no artificial metadata-header or packet-size cap. Preserve every sensor sample, clock, directional FTM report, event and diagnostic. Never compact or truncate observations to fit a byte budget.
The four-byte metadata length describes the actual encoded JSON header. Validate that the declared length fits the received frame; this is framing validation, not a size policy.
The camera stream remains latest-only. A slow consumer may discard superseded frames without editing the frame it receives. Epoch, authority, ordering and driving-intent validation still apply.
The legacy bounded_metadata maximum argument and older grant max_message_bytes values do not impose telemetry size limits. Nontelemetry stream contracts keep their own policies.
