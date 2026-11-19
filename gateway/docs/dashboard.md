# Hosted controller dashboard

Open https://biomerte-controller.cantikash.chatgpt.site/dashboard/ and sign in with the existing controller owner ChatGPT account. The gateway serves the existing controller interface and forwards its JSON bus calls over the established outbound controller connection. Both realms, cached reads, request IDs, domain errors, and all tabs are retained. The controller operator credential remains on the controller host.

The public site audience and existing MCP/OAuth routes are unchanged. Dashboard assets and API are owner-only; POSTs require the same origin and JSON. No general HTTP proxy or caller-selected destination is exposed.

Deploy the matching deploy/gateway-agent.py to each controller before that controller serves hosted dashboard calls. Static assets are a snapshot of the deployed controller UI; regenerate when the UI changes. The original loopback dashboard remains available.
