# BiomeRTE Controller MCP

ChatGPT's native BiomeRTE Controller plugin uses `/mcp`. Grok and Muse use
`https://biomerte-controller.cantikash.chatgpt.site/api/mcp` with OAuth.
Grok can dynamically register. Muse uses public client ID `meta-muse`,
an empty secret, and its registered Meta callback. The signed-in Site owner
approves access. Revocation is available at `/oauth/connections`.

The four tools read controller health, list work, discover capabilities and
invoke an operation. Use the same request ID for retries. Operational tool
results come from the local controller. The outbound agent connects over TLS
using a separate device key. The controller operator token stays on its host.

The gateway's D1 database stores OAuth grants and transient calls. The Pi is
the system of record. Configure BIOME_OWNER_EMAIL, BIOME_OAUTH_ORIGIN and
BIOME_BRIDGE_PIN before deployment. The public verifier is printed by
`deploy/gateway-agent.py --pin --origin https://your-host`.

MCP calls, provider WebSocket messages, native signed calls, provider pairing,
decisions and dashboard operation requests have no gateway-imposed whole-request
byte or character cap. The former 125000-byte ingress, 96000-character queue and
proposed 1 MiB replacement ceilings are removed, not raised. Requests and signed
JSON are forwarded without truncation. Authentication, signature verification,
operation schemas, queue capacity, immutable request identities and controller
lease fencing remain unchanged. Platform memory, HTTP/WebSocket and database
storage constraints still apply; this is not a promise of unlimited hosting.
Existing bounded result frames are transport framing, not logical result limits.

Provider transport failures emit one `provider_gateway_failure` event to the
existing Worker logs, with a versioned schema, stage/reason, generated bridge
job ID, validated operation, elapsed time, polling state and numeric status.
Queue rejection, queue/database errors, an unanswered waiter, controller errors,
and response decoding/serialization failures are distinct. A waiter timeout
means no reply was observed; it does not prove a command did not execute.
Controller error text is never copied: only fixed, allowlisted error categories
are recorded. Request IDs, signed envelopes, arguments, credentials, response
bodies and raw exception messages are omitted. Success adds no diagnostic event.
This adds no database/log table, journal, payload copy, cleanup change or retention
extension; existing platform log retention and transient bridge retention remain.

Authenticated `controller.diagnostics` also reports active bridge work grouped
by state, controller and normalized operation. It includes live controller
claims after their dispatch deadline, because those claims still occupy
capacity. Output is bounded to 64 groups with complete totals and an explicit
truncation flag. Ages measure time since the last bridge-row update, not native
execution or physical response. The aggregate contains no request bodies or
credentials. Poll counts combine transport paths and do not identify which
path produced an individual request.

For provider WebSockets using the replicated site realm, an empty native inbox
reply waits for a shared journal change before another native poll. The journal
baseline is captured before each poll so a change during that poll cannot be
missed. A five-second fallback starts after the empty reply; local, legacy or
unavailable journal signals retain ordinary polling. The journal is only a
wake-up hint: inbox contents and authorization still come from native calls,
bearer authorization is checked while waiting, and closing the socket stops new
polls. Signed HTTP provider calls retain their existing behavior.
