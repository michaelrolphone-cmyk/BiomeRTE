# Existing native protocol through the public gateway

POST `/v1/call` with `Content-Type: application/json` and the existing native
`{site,id,realm:"site",operation,arguments,auth:{actor,signature}}` body. The
original UTF-8 JSON is forwarded unchanged to configured loopback `/v1/call`.
No public-key wrapper, device allowlist, new enrollment authority, index,
migration, acknowledgement endpoint or credential creation is introduced.
Existing native peer enrollment and scope checks remain authoritative.

Every HTTP attempt gets a new transport job, so no gateway-cached result can
bypass current controller signature, scope or revocation checks. Native
actor/request-ID mutation receipts deduplicate uncertain writes. Network loss
or HTTP503 requires retrying the IDENTICAL native request body/ID, never a new
native ID. Controller200 and4xx bodies/statuses are preserved. Backend5xx,
queue saturation, bridge timeout and malformed transport results become
retryable503. Reads can run again after response loss, as in the native API.

The existing shared bridge40-job admission,20-second HTTP wait,22-second
transport eligibility and hourly cleanup are unchanged. These constrain bridge
attempts, not native command receipts. Controller mutation recovery relies on
its durable receipts. Requests must fit the existing125000-byte HTTP budget and
96000-character serialized bridge-job budget; exceeding either returns413.
Existing framed result delivery is unchanged and no new response cap is added.
Only site realm and the configured site are accepted. Caller-selected target
nodes/URLs and operator credentials are never forwarded; redirects are refused.

The public ingress only validates structure before queueing. Invalid but
well-shaped signed requests can consume shared bridge capacity and controller
verification CPU. This minimal change provides no separate ingress rate limit,
per-device fairness, flood protection or thousand-device capacity guarantee.
The platform's public reachability/access gate must be verified at rollout.
Pairing/commissioning endpoints are not exposed by this change; enrollment uses
the existing controller commissioning path. No new remote enrollment protocol
is implied by adding transport.

Integration: include `deploy/native_call.py` alongside each active connector
and add the three-line `biome_native_call` dispatch branch shown in
`deploy/gateway-agent.py`. Preserve current live connector fixes, particularly
Pi's removed pre-framing fetch guard; do not replace live connector files with
an older full source file. Publish only after separately authorized native
Mac/Pi qualification and public end-to-end checks. This artifact is source-only.

Checks from `gateway/`: `node --test tests/*.test.mjs`, `npm run build`,
`npm run validate`. From the repository root: `python3 -m unittest discover
-s tests -p 'test_native_call*.py' -v`. The real controller test defaults to
this repository; `BIOMERTE_TEST_ROOT` can select another controller checkout.
The real controller test verifies scope rejection, forged signature rejection,
native mutation receipt replay and revocation on retry. The real loopback helper
test verifies bytes, UTF-8 responses and absence of privileged headers.
Python helper/harness use Python 3.4-compatible APIs.
