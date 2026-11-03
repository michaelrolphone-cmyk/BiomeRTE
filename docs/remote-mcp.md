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

## Delegate a workflow step to ChatGPT

Add a `delegate` step with `to: "chatgpt"` where the workflow needs judgment.
The step binds its `request` and `context` using the normal workflow expressions,
declares a typed `outputs` object, and saves the returned object with `save`.
A BPMN `serviceTask` can use `biome:delegate="chatgpt"` with the same bindings.
See [the complete routine](../examples/chatgpt-delegation.json).

Subscribe in ChatGPT to **workflow.delegation.requested**. An optional
`workflow` filter selects an exact workflow name. The `realm` defaults to
`site`; use `local` for isolated work. A new subscription enumerates requests
from the beginning unless a cursor is provided. State on the controller is
authoritative, so skip requests that are already resolved or cancelled.

For each event use `biome_invoke` in its realm:

1. Read `delegation.get` with the event's `delegation_id` as `id`. Read the
   request, context, and expected result, and check that the run is still active.
2. Call `delegation.claim` with `id` and a unique `claimant` for the worker
   execution. Reuse that identity across retries. If another worker has claimed
   the step, leave it there.
3. Perform the requested work with the user's existing permissions. Return
   `delegation.complete` with `id`, the same `claimant`, and the typed `result`,
   or a nonempty `error` to take the workflow's error branch. The engine resumes
   from the correlated wait. `delegation.release` allows deliberate reassignment
   after an interrupted worker's effects have been reconciled.

The gateway advertises events on both MCP transports. The host persists
subscriptions and cursors alongside its outbound link key. It verifies callback
ownership and signs webhook deliveries. Transient failures retry with the same
event ID; the receiver's successful HTTP response acknowledges delivery only.
The workflow continues to wait for the result.
