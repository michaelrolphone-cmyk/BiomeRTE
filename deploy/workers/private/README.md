# Private chat and image worker containers

These images contain the actual Python workers: signed ephemeral sessions,
decryption/encryption, one-use request handling, durable hash/fence/status ledgers,
progress encryption, provider polling, and large image artifact chunk delivery.
They do **not** contain or migrate LM Studio, ComfyUI, MLX, model weights, or
Mac Metal execution. Both workers use the existing authenticated HostIO boundary
to the same Mac. A different host still needs a separately qualified model
backend; these images alone do not provide GPU failover.

The qualified target is Linux ARM64 on this Mac's Docker Desktop. No ARMv7 or
CPU inference claim is made. AMD64 wheels may exist but that platform has not been
qualified by this handoff.

## Ownership and privacy

Native ModelRuntime owns the native admission SQLite database, Mac process
identities and flock. The bridge loads the preserved model implementation modules
from absolute, SHA-256-pinned paths and calls the real native admission context.
The Docker images do not mount the native admission database, model tokens,
weights, OAuth files, ComfyUI source, or native private runtime directories.

A lost container connection only loses delivery. The native call continues through
cleanup under the same admission slot, and the next request stays queued.
Model calls never retry automatically. The existing worker ledger records only
hashes/fences/status and fails an interrupted operation instead of replaying it.
Results awaiting gateway acknowledgment remain ciphertext in RAM.

Model payloads require TLS, the existing HostIO bearer credential, and a pinned
registered host identity. The existing HTTP listener can continue to serve
clock/routes/vision. An optional additional TLS listener shares the **same**
HostIO process and backend:

```json
{
  "tls_listener": {
    "listen": "127.0.0.1",
    "port": 8766,
    "tls": {
      "cert_file": "/absolute/native/server.crt",
      "key_file": "/absolute/native/server.key"
    }
  }
}
```

When that listener is configured, gateway.invoke and gitlab.token also require
TLS. The model stream always requires TLS. Certificates and private keys are
provisioned outside the repository/images; all TLS clients trust the local CA.
Client configuration uses the existing schema:

```json
{
  "url": "https://host.docker.internal:8766",
  "host_id": "<existing registered host>",
  "ca_file": "/run/biome/host-io-ca.pem",
  "token_file": "/run/biome/host-io.token"
}
```

The native process handles LM Studio privacy checks on **every** private request:
the existing privacy qualification marker and both sensitive logging flags must
remain disabled. Exact requested model selection, resolved-instance verification,
store:false, retained-response rejection and diagnostics remain intact. ComfyUI
uses the existing loopback-only backend and RAM image node, accepted parameter
allowlist, progress stream, prompt-history deletion and memory cleanup.

Image progress goes back to the worker and is encrypted in the existing image
session before agent.progress. Large output is framed over TLS with SHA-256
verification, then encrypted/chunked by the existing artifact protocol.
A tmpfs health receipt contains only a request UUID, container PID and start time.
The private health check accepts successful gateway polling/progress/artifact delivery or that exact request still
present in native model custody; a long image generation is not a stale poll.

## Build and qualify

From a clean source checkout:

```sh
/usr/local/bin/docker build --platform linux/arm64 --target private-chat -f deploy/workers/general/Dockerfile -t biomerte-private-chat:qualified .
/usr/local/bin/docker build --platform linux/arm64 --target private-image -f deploy/workers/general/Dockerfile -t biomerte-private-image:qualified .
python3 -m unittest discover -s tests -p test_model_host_io.py -v
python3 -m unittest discover -s tests -p test_host_io.py -v
python3 -m unittest discover -s providers/private-chat -p 'test_*.py' -v
python3 -m unittest discover -s providers/private-image -p 'test_*.py' -v
```

The model qualification uses synthetic model responses and the real native FIFO
admission implementation in an isolated temporary directory. It covers TLS/host
binding, native privacy checks, exact model selection and response mismatch,
disconnect custody, FIFO ordering, shutdown drain, encrypted progress, large
artifact transport, cleanup, interrupted stream failure without replay, and
absence of private content in persistent fixture files. It is not a physical
Android or production model-generation test.

Native HostIO's Python environment needs aiohttp for existing ComfyUI progress;
the Linux workers need cryptography, not the native inference dependencies.

## Prepare without activating

Both workers share the signing identity and paired phone in the existing chat
state directory. They must move together. Never run their install/reset-pairing
commands. A new deployment directory is prepared using immutable local image IDs
and an existing HTTPS client configuration with native-readable CA/token paths:

```sh
python3 deploy/workers/private/prepare.py --output /absolute/deployment   --client-config /absolute/native-client.json   --chat-image sha256:<qualified-chat-id>   --image-image sha256:<qualified-image-id>
```

The command writes compose.json, a container client config and separate native
client config, a **models-only** bridge patch with exact source hashes, native
LaunchAgent rollback copies, and plan.json. It validates the actual image
architecture and Docker Compose specification. It does not start a container,
stop a native worker, mutate gateway definitions, enroll a phone or change HostIO.

Merge config/bridge.models.patch.json into the latest HostIO config, preserving
gateway/gitlab/vision/anchors and concurrent settings. Stage the bridge as the
owner's normal isolated candidate first. models.status must report those same
source hashes. The helper reads the existing native privacy/model configuration;
none is synthesized by preparation.

## Cutover owned by the operator

1. Check handoff.py DEPLOYMENT status. Require both worker ledgers reconciled,
   no native model admission and no active bridge request. Do not interrupt an
   existing conversation request, generation or artifact delivery.
2. Disable and boot out both org.biomerte.private-chat and
   org.biomerte.private-image under gui/<native uid>. Confirm both native PIDs
   exited. Docker Desktop and native macOS flock locks do not fence each other.
3. Run handoff.py DEPLOYMENT capture. It refuses any native worker, same-state
   container writer, queued native inference, unresolved private action, changed
   identity or source mismatch. The snapshot includes the actual signing key,
   pairing/config state and a consistent backup of each stopped SQLite ledger.
   Snapshot files are mode 0600. This is not a distributed atomic checkpoint.
4. Start both images together:

   `/usr/local/bin/docker compose -p biome-private -f DEPLOYMENT/compose.json up -d --no-build`

5. Run handoff.py DEPLOYMENT verify after health becomes healthy. It requires
   exactly those two immutable image IDs, no native owner, preserved signing/
   pairing/provider identity, and the pinned native model implementation.
   Then qualify the authorized application exchange through the gateway/phone.

Preparation and synthetic qualification are not a completed live migration.
verification.json explicitly records that the model backends stay native and
that it does not claim an Android-device test.

## Rollback

Drain active requests first using status; native model calls outlive a dropped
container connection and must finish cleanup. Stop **both** private containers
with the same Compose project. Confirm they exited before enabling/bootstraping
the two captured native LaunchAgents. Keep the current shared state and the
existing identity; do not blindly restore an older ledger or pairing snapshot.
The snapshots exist for diagnosed corruption, with an explicit choice to restore.

No private native source or LaunchAgent is changed by the image build. The
original workers remain available at the captured paths. Bridge shutdown stops
both listeners before draining in-flight model calls. The external supervisor
can still force-kill a process, so a real restart must wait for models.status
active_requests=0; a grace timeout is not permission to abandon native custody.
