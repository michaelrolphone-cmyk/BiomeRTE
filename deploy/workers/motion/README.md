# Actual motion worker images

This image contains the deployed rover motion implementation: persisted model,
prediction, inverse planning, training sessions, observation recorder, and capability
provider. It includes the same adaptive model and durable session behavior as the
native releases. The observer and provider run as separate processes from one image.

The imported source came from the native provider release
`c4606f5a41bf7fa3`, observer release `motion-events-94c150d03b74`, and their
matching runtime modules. Original source hashes are recorded in
`../vision/source-import.json`; those hashes describe the source before portability
changes. The existing repository's worker custody protocol remains in use.

## Build and qualification

From the repository root:

```sh
docker build -f deploy/workers/motion/Dockerfile -t biomerte-worker-motion:<source-sha> .
docker run --rm --network none --read-only --tmpfs /tmp biomerte-worker-motion:<source-sha> qualify
```

The image has no embedded OAuth token, provider identity, model database, or rover
bearer token. Qualification exercises actual model, planning, training and provider
implementations with temporary fixtures. It does not register a provider or command
physical motion. The image is architecture specific; build separately for each
supported Linux architecture.

## Runtime configuration

Render new manifests from the existing native provider manifest:

```sh
python deploy/workers/motion/render-config.py --native-manifest /path/to/native/provider.json --rover-origin http://host.docker.internal:8093 --output-directory /path/to/container-config
```

This preserves provider/site/resource identity, worker state, model database and
capacity. Only executable and module paths change to the packaged implementation.
The observer uses the native applied-control stream through the supplied origin and
token file. Vision snapshots, sonar and the continuous vision feed cross the existing
authenticated HostIO boundary. The client translates native monotonic timestamps to
the container epoch and retains uncertainty/source-clock evidence.

Set all variables required by `compose.yaml`. `MOTION_PROVIDER_STATE` is the
provider manifest's `state`; `MOTION_DATABASE_DIRECTORY` is the database's parent.
Both mounts retain the original absolute paths. The container HOME and
BIOME_PROVIDER_HOST must match the native Mac placement identity. The packaged
gateway transport delegates to the native credential owner through
`/run/biome/host-io.json`; mount only that client config and its bearer token.
Do not mount native rotating OAuth files.

## Handoff

The deployment owner must stop the native observer and provider before starting the
container equivalents, and inspect existing native database consumers. macOS and
the Docker VM do not share file-lock enforcement. The new observer's exclusive lock
prevents a second container observer; it cannot exclude the older native process.
Keep both motion writers/readers in the same VM, or route native consumers through
the existing native/API boundary instead of concurrently opening the SQLite/WAL
files from both kernels.

Preserve the complete database/WAL custody state and provider worker state during
handoff. Do not copy an active SQLite database as a single file. Rollback first stops
both containers, then restores the native ownership arrangement. No native service
stop/start or cutover is performed by the build, qualification, or renderer.
