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

## Let Grok attach a webhook receiver

Connect Grok to `https://biomerte-controller.cantikash.chatgpt.site/api/mcp`
using OAuth. In the Grok routine, add a webhook trigger and save it. Ask Grok
to register its own **POST to** URL and **key** with
`biome_register_grok_webhook`. Supply the key as a private tool argument.
The URL path and key are omitted from management receipts.

| MCP tool | What Grok supplies |
| --- | --- |
| `biome_register_grok_webhook` | Stable `receiver` name, full `url`, raw `key`, and a new `request_id`. Optional `realm`, `topic`, `workflow`, `auth_mode`, and `from_cursor`. |
| `biome_list_grok_webhooks` | No arguments. Returns filters, origin, cursor, last accepted delivery time, and last error for this connection. |
| `biome_unregister_grok_webhook` | The same `receiver` and `realm`, and a new `request_id`. |

Clients that already cached the original tool list can also call `biome_invoke`
with `grok.receiver.register`, `grok.receiver.list`, or
`grok.receiver.unregister`. For these aliases, pass the normal outer `realm`
and `request_id`. They use the same authorization and protected receiver store.

The default mode, `bearer`, sends Grok Bot's `Authorization: Bearer` header.
The separate Grok Automations product issues a `whsec_` signing secret;
specify `auth_mode: "standard_webhooks"` for that trigger. Always use the
URL generated by the routine. Registration does not post to the destination.
Use a new request ID when rotating credentials or editing filters; reuse the
original request ID only to retry the same operation. Re-registering against
the same feed retains its cursor. Unregistering removes the destination key.
OAuth revocation stops delivery.

The default event is `workflow.delegation.requested` in the `site` realm.
A Grok receiver gets delegations addressed to `"to":"grok"`. Existing
ChatGPT subscriptions continue to receive `"to":"chatgpt"`. In the routine,
read `delegation.get`, confirm the run is live and the request is addressed
to Grok, claim it with a unique worker identity, then submit the typed result
using `delegation.complete`. A webhook acceptance only starts the Grok run.
See the [Grok workflow example](../examples/grok-delegation.json).

A receiver can instead name an exact controller topic such as
`process.completed` or `user.sensor`. Give each independent subscription its
own receiver name. A new receiver begins with future events. An explicit
`from_cursor` replays after that sequence, and an optional exact `workflow`
filter is available for delegation events. Generic notifications include
the realm, event ID, topic, sequence cursor and a compact payload. Large payloads
can be read with `bus.event_get` using their sequence and realm.
Bearer deliveries include a stable `Idempotency-Key`; use `eventId` to
deduplicate retries. A retired endpoint or rejected credential pauses Grok
delivery at the unacknowledged cursor until it is updated.

For a separately hosted gateway, configure `BIOME_RECEIVER_WRAP_KEY` as a Site
secret containing a base64-encoded random 32-byte AES key. It protects
registration arguments in the gateway's transient queue. The destination
credential is stored in the controller user's private receiver database.

## Receiving a lease grant

For ChatGPT, subscribe to the `lease.available` MCP event with an exact `reply_to` address and optional realm. For Grok, register a receiver for topic `lease.available` with the same optional inbox filter. Set that address on `cap.acquire` before waiting; the data contains the lease ID (`id`), task, contract, resource and generation. Re-read `cap.lease` on wake before invoking. Read `cap.status` with the subsequent command ID for its result. See the [capability caller guide](runtime.md#calling-a-capability).
