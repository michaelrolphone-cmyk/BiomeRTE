# Connect an MCP client

The deployed gateway is https://biomerte-controller.cantikash.chatgpt.site/api/mcp.
Use OAuth, complete the owner sign-in and consent, and follow **Continue to your app**.
Grok supports dynamic registration. Muse uses client ID `meta-muse`, no secret,
and its exact registered callback. For ChatGPT, use the BiomeRTE Controller
plugin or the same external OAuth URL as a custom MCP. Revoke an external
grant at https://biomerte-controller.cantikash.chatgpt.site/oauth/connections.

Start with `biome_status` and `biome_work_list`. `biome_discover` returns live
operation contracts. `biome_invoke` calls an operation on your controller.
Give it a request ID; reuse that ID with exactly the same inputs after a lost
reply. Results are fetched from the physical controller.

## Host the controller

On a Linux systemd host, run the following as the user that owns the checkout.
The installer uses sudo for the units. Each deployment connects a single
controller to the hosted gateway. Configure its hash explicitly when switching
hosts.

    sh deploy/install-gateway-linux.sh https://biomerte-controller.cantikash.chatgpt.site

The connector initiates HTTPS requests from the host; no inbound router rule is
needed. Systemd starts the controller and connector on boot. The controller
listens on loopback. The link key is separate from the local operator token.
Configure only the public verifier in the gateway's BIOME_BRIDGE_PIN setting.

    python3 deploy/gateway-agent.py --pin --origin https://biomerte-controller.cantikash.chatgpt.site
    systemctl status biomerte.service biomerte-gateway.service

The same Python connector runs on macOS in an account with the controller,
a private 64-hex-character link key and the configured HTTPS origin.

For an owner-private Site, the agent can read a mode-0600 `site-access.json`
beside its dedicated key. It contains the exact `origin` and that Site's
platform service-access `token`. The agent sends the extra header only to that
origin over HTTPS. Application-level device authentication is still required.
