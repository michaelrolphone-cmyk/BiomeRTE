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
`../deploy/gateway-agent.py --pin --origin https://your-host`.

Run the test suite with Node 24 using `node --test tests/*.test.mjs`. For a separate deployment, copy `hosting.example.json` to `.openai/hosting.json` and set the new project ID. Build with `bash scripts/build.sh`. Deploy the Worker with its D1 migrations.
