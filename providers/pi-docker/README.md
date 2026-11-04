# Pi Docker CLI

`docker.cli:1` runs the installed Docker client on the provider host. Supply exactly the argument tokens that follow `docker`. Global flags, command groups, templates and subcommands are interpreted by that client. Access is intended for trusted operators: control of the Docker daemon grants administrative access to the host.

| Command | Capability inputs |
|---|---|
| Show client and server versions | `{"argv":["version"]}` |
| List all containers with selected fields | `{"argv":["ps","-a","--format","{{.ID}} {{.Names}}"]}` |
| Build from a directory on the Pi | `{"argv":["build","-t","example","."],"cwd":"/home/pi/project"}` |
| Build from a Dockerfile on stdin | `{"argv":["build","-t","example","-"],"stdin":"FROM scratch\nLABEL example=true\n"}` |
| Execute a command with piped input | `{"argv":["exec","-i","my-container","cat"],"stdin":"hello\n"}` |
| Load or save a large archive on the host | `{"argv":["load","-i","/path/image.tar"]}` or `{"argv":["save","-o","/path/image.tar","my-image"]}` |

Read the offer's `usage` in `biome_discover`, or query `cap.discover` for `docker.cli:1`. Acquire a lease, wait for `bound`, invoke it with a new operation ID, then follow `next_call` to `cap.status`. Release the lease after the command finishes. Docker accepts multiple operations on independent leases.

Use one stdin source: UTF-8 `stdin`, `stdin_base64`, or ordered `stdin_chunks` containing artifact SHA-256 IDs or `{sha256,size}` entries. For gateway uploads, split input into chunks that fit the MCP request size (48 KiB raw is suitable), use `artifact.put`, then pass the resulting references. Chunked input is limited to 128 MiB. Host files and build contexts can be larger. The provider keeps its own Docker client configuration.

A transport success means Docker returned an outcome. Check `result.executed`, `exit_code` and `timed_out`. Standard output and error have separate 4 KiB previews. Their ordered artifact references, digests, captured size, total bytes and truncation flags are in `result.streams`. Each stream captures up to 16 MiB. Use `artifact.get` for exact bytes. Calls have finite stdin and return on completion, without a live terminal attachment. The default timeout is 600 seconds, configurable to 3600. Timing out the local client can leave work already accepted by the daemon running.

## Install

From this checkout, run `python3 providers/pi-docker/install_docker.py` as the non-root BiomeRTE controller owner. The installer expects a boot-managed Docker daemon and a writable Unix socket assigned to a dedicated non-root group (normally `docker`). Use `--daemon-unit`, `--executable`, and `--socket` to select the installed setup. Only the provider service receives the supplementary socket group. `--slots` sets the pool size (default 4). Keep the checkout available at its installed path.

The provider's key and manifest live in `~/.local/share/biomerte-docker`. Its worker uses the usual BiomeRTE fencing and durable operation ledger. Check the unit with `systemctl status biomerte-docker`.
