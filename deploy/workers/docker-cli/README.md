# Mac Docker CLI worker image

The release catalog builds `worker-docker-cli` for Linux arm64 using the
current BiomeRTE worker library, Mac provider, and shared Docker CLI adapter.

The worker expects its existing Docker CLI manifest and provider identity
under the container's home directory. Mount the existing Docker worker state
at `$HOME/.local/share/biome-mac-docker` with the same user and ownership.
For the gateway, provide the existing Docker provider's machine identity
configuration and key at `/run/biome/mac-terminal`. Use a dedicated scope
for that provider. Do not mount the complete Mac terminal state directory. In the Docker CLI
manifest, set `executable` to `/usr/local/bin/docker`, set `socket` to the
in-container Unix socket path, and expose the Mac Docker Desktop socket at
that path. Retain the existing site, provider and resource identities.

Stop the native worker before starting the container with that identity.
The release smoke check loads the worker imports and runs `docker --version`
without contacting a Docker daemon or controller.
