# Runtime guide

## Work and execution

`work.create` creates goals, milestones, planning workflows and tasks. A parent, when supplied, must be the appropriate hierarchy level. Dependencies gate readiness. Priority orders ready tasks, with planned sequence and ID as tie breakers. Work edits compare the caller's revision against the current record. The BPMN `select` activity starts one ready task with the selected procedure. A successful run moves its task to **review**. `work.accept` takes separate evidence for every criterion. `work.reopen` preserves previous evidence and allows another attempt.

Process definitions are immutable by digest. A calling process pins its child revisions when published. `process.start` records the chosen revision, inputs, token state and task identity. A bounded tick persists each activity and pending provider action atomically. The runner resumes after restart. `process.migrate` takes the expected revision, target revision and an explicit mapping for live nodes. It rejects moving tokens in a pending call, external wait, loop or parallel join. `process.cancel` marks unstarted requests cancelled and preserves uncertainty about dispatched effects.

### Import BPMN

From the checkout:

```sh
python3 -c 'import json; print(json.dumps({"name":"review","xml":open("examples/review.bpmn").read()}))' > /tmp/biome-review.json
./biome call bpmn.import @/tmp/biome-review.json
./biome call process.start '{"name":"review","inputs":{"item":"A field report"}}'
./biome call process.waits
```

With the server running, use Attention to answer the user task. The import supports a single process, start/end events, data assignment tasks, service tasks, user/manual tasks, receive tasks and message or duration catches, exclusive gateways, structured parallel splits/joins and call activities. Data bindings use the `urn:biomerte:v1` XML namespace. Gateway expressions and arguments are JSON. Unsupported activity types are rejected. It is an executable profile, not a complete BPMN 2.0 modeler.

A message or human wait has its own ID and output schema. Submit `process.answer` with that ID and an answer object. A duplicate identical answer returns the earlier outcome. Different answers conflict. Due-after timers use elapsed monotonic time. A controller that first observes a pending timer after restart starts a conservative local delay. Ordering otherwise uses revisions, writer sequences and committed journal positions.

### Routines

`routine.publish` accepts input and output type maps, a list of steps and a result expression. Types include str, int, num, bool, obj, list and any. A trailing question mark makes a field optional. `{"$":"input.value"}` reads a scoped variable. An expression such as `{"op":"ge","args":[{"$":"peak"},{"$":"input.threshold"}]}` compares values. Steps can call a provider, set variables, invoke another routine, branch with if/then/else, or iterate with each/as/do. Child calls isolate their variables. A fork merges independent outputs at its join and reports conflicting values. No host-language eval is used.

The checked-in radio survey illustrates callable measurement, iteration, state accumulation and conditional capture. Publish the contracts and two routines:

```sh
./biome call cap.define '{"name":"radio.measure","inputs":{"frequency":"num"},"outputs":{"dbfs":"num"}}'
./biome call cap.define '{"name":"radio.capture","inputs":{"frequency":"num"},"outputs":{"reference":"str"}}'
./biome call routine.publish @examples/measure.json
./biome call routine.publish @examples/branching-survey.json
```

A real receiver must offer these operations before the survey can finish. If a contract or matching implementation is missing, the waiting activity links to a deduplicated construction task. When an implementation appears the same activity resumes. Temporary allocation conflicts wait for a resource without creating construction work. A try/catch block can recover from a failed subroutine while retaining successful readings. The composite example in `examples/survey-and-display.json` returns the survey and the display outcome separately. The test suite uses a fixture receiver, including both capture and skip branches. `routine.learn` extracts successful provider commands from a run trace and replaces selected inputs with parameters. Edit the recipe to express intended conditions or loops. `routine.expose` publishes a pinned routine as a capability offer, so other callers use exactly the ordinary lease and invocation API.

### Providers and agents

1. Initialize the worker using a private `BIOME_HOME`; run `./biome identity` on the device. Its fingerprint identifies its public key.
2. Over a verified TLS connection (or an SSH loopback tunnel), run `./biome pair https://controller:8740 --ca site-ca.pem --label receiver`. It returns a challenge and shows the fingerprint. On the controller, compare the candidate key with the physical device's identity over an independent trusted path, such as the console or commissioning cable. LAN discovery with `./biome discover` only locates candidates.
3. Approve that exact fingerprint with `pair.approve`. A minimal command worker needs `cap.offer`, `provider.poll`, `provider.claim`, `provider.result`, `cap.fenced`, an `offer:CONTRACT` scope for each allowed contract, and a `resource:RESOURCE` scope for each permitted resource. Add `cap.configured` to enable activation acknowledgements, `agent.progress` for checkpoints, and `cap.status` for reconciliation. A direct public-key enrollment using `peer.trust` is available for a physically commissioned peer. The controller TLS certificate or tunnel is verified independently of application peer identity.
4. Run the same pair command with `--finish`. The worker proves possession of the private key. Its application credential is the scoped key identity. Subsequent requests sign the site, realm, operation, arguments and request ID. Revocation is checked on each request. Local realm grants are explicitly managed in that realm.

Example manifest for a provider on the same machine, with absolute paths appropriate to the worker:

```json
{
  "url":"http://127.0.0.1:8740",
  "site":"SITE_ID_FROM_PAIRING",
  "realm":"site",
  "offers":[{"contract":"device.echo:1","resource":"echo-device","argv":["/usr/bin/python3","/absolute/BiomeRTE/examples/echo_adapter.py"]}]
}
```

Create `device.echo:1` with input and output `{"value":"any"}`. Run `./biome worker /path/to/manifest.json` on the enrolled worker. A worker with configurable functions can advertise `state: "available_after_setup"` and a trusted `configure_argv`. `cap.prepare` requests activation and the process runner requests it automatically when a service cannot bind. A successful setup probe makes the offer ready. A failed activation retains its evidence and waits for a corrected offer before another attempt. Setup commands implement idempotent desired-state application. Agents can supply any registered contract and checkpoint a claimed activity with `agent.progress`. Addressable inboxes and exact event subscriptions are also available.

An adapter receives one JSON object on standard input containing its stable operation ID and inputs. It returns its contract's result object on standard output. The manifest supplies a fixed executable and argv; input values never become shell commands. Run the worker under a dedicated account with only the underlying device or service permissions it needs. Provider implementations are trusted native programs.

### Resource ownership and uncertain effects

A lease is stable for a task/contract/resource request. The controller persists a resource generation, and the provider durably installs the generation before it can claim an operation. A physical resource has one enforcement point. Commissioning another provider under the same physical resource ID is rejected. Substitution to a different compatible resource is possible at a safe command boundary. Outstanding queued, claimed or uncertain operations prevent the old reservation from being released.

The host worker locks its ledger and writes an **uncertain** record before calling a command. It persists the finished result before reporting it. After losing a reply it resends the saved result without repeating the command. If it crashed during the command, inspect the real effect first, then use `resolve-effect MANIFEST OPERATION succeeded JSON` or `failed JSON` to reconcile. Keep that ledger with the device. If a device has independent control paths, it needs a shared native fence across all of them before it can safely exchange ownership.

### Construct and deploy an adapter

`artifact.put` stores a base64 chunk by SHA-256; large objects can use manifests of chunks. `package.stage` links a Python adapter artifact to its contract and sample input/expected-output probes. An agent may stage code when granted those operations. To activate reviewed code on a host, run `./biome install-package PACKAGE_ID RESOURCE /path/to/worker-manifest.json` using a controller with the staged artifacts available. It verifies the digest, executes the probes and atomically updates the manifest only on success. Restart the enrolled provider to publish the new offer. Existing input/output schemas apply to this adapter. Probes run with the installer's OS privileges, so review code or install in an isolated account. Adapters that deploy to other machines can expose their own install/probe contract using the same provider protocol.
