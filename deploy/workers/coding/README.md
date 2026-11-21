# Actual coding tools container

This image runs the installed coding MCP frontend, official filesystem tools and actual durable Python job coordinator. Its source is captured from the Mac's active `biome-lmstudio` coding deployment, plus the GitLab and workspace tool helpers. This is the implementation used to read/write coding files and submit/reconcile terminal, Git and workspace operations.

It does not contain LM Studio, the LM Studio agent, MLX, ComfyUI, a model, or native memory admission. Those engines can use this MCP endpoint as clients. The Python coordinator still executes commands through existing Biome capability providers; it does not run a new native terminal or a Docker daemon.

## Source and dependencies

`source-provenance.json` records the installed source SHA256 and packaged SHA256 for each captured file. The Node dependency lock is copied unchanged: filesystem SDK 2.1.0, MCP server/core 2.4.0, and their exact transitive integrity hashes. This filesystem SDK requires Node 24 or newer. The Docker base is pinned to the observed Node 24 Alpine manifest digest.

The code adaptations replace the Mac-specific Python executable and import-directory literals with the image's Python and explicit code/state paths. Default native state locations remain available in the source. The actual command state machine, archive format, operation IDs, reconciliation sequence, SDK tools and MCP route behavior are retained.

The common `mac_provider` gateway client and `HostIOClient` send requests through the supplied authenticated HTTPS host owner. The native owner retains OAuth credentials. No OAuth file, refresh token, inference memory state, signing identity or live command record is copied into the image. Workspace metadata helpers reuse the existing portable manager/provider modules for their registry and request routing; this process does not run their worker loop.

## Build and qualify

From the repository root:

```sh
docker build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" -f deploy/workers/coding/Dockerfile -t biomerte-worker-coding:"$(git rev-parse --short=12 HEAD)" .
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,noexec,size=32m,mode=1777 IMAGE_ID
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,noexec,size=32m,mode=1777 --entrypoint python3 IMAGE_ID -B -m unittest discover -s /app/tests -p 'test_*.py' -v
```

The default command checks actual Python and Node dependencies without reading credentials or starting a server. Image tests start the real Node MCP service only on isolated loopback with fixture state. They verify bearer/origin/Host checks, official filesystem reads and confinement, all eight Biome tool registrations, the real Node-to-Python dispatch path, durable lost-reply handling and retained nonzero command results. They neither acquire live capabilities nor execute an external command.

## Preserve the existing endpoint and durable state

Use the existing `coding-config.json`, `mcp-token`, `mcp_port`, workspace root and resource IDs. The frontend still requires the exact `127.0.0.1:<mcp_port>` Host header, the matching optional Origin, the existing bearer token, and `/mcp`. Inside the container it listens on 0.0.0.0 so the Compose file can publish that port on **host loopback only**. The native LM Studio MCP endpoint configuration can remain unchanged.

Mount the workspace and command records at their existing absolute paths. Saved jobs contain absolute archive/output/workspace paths; moving those paths or discarding old records would break reconciliation. Bind only these specific resources:

- Coding configuration and MCP token read-only; coding `commands` directory read-write.
- Existing canonical coding workspace read-write, as required by filesystem tools.
- Git tool catalog read-only; Git `agent-commands` directory read-write.
- Workspace provider/schema metadata read-only.
- Supplied HostIO client directory read-only at `/run/biome`.

The HostIO configuration must specify HTTPS, the existing registered host ID, `ca_file` and `token_file`. The final owner endpoint is supplied by the host rollout, with the local CA and token mounted separately from source. `check-config` verifies these files without invoking the owner. If using the native terminal mode instead of the existing Docker job mode, explicitly retain that target host's tool PATH through `terminal_tool_path` or `BIOME_TERMINAL_TOOL_PATH`; container Python paths must not be confused with the remote terminal's paths.

Set all required Compose variables from the current installation. Select an immutable qualified image digest and the correct host UID/GID. No Docker socket, host process namespace, hardware device or broad home-directory mount is required.

## Handoff and rollback

Before cutover, stop new MCP admissions and let in-flight native MCP/Python requests finish or retain their original durable IDs for reconciliation. Stop only the native coding frontend owner. Do not run native and container owners simultaneously against these command directories: file-lock semantics must not be assumed across Mac and Docker VM writers.

Start the container with the same configuration, port, token and state mounts. Reconcile existing operations by their original IDs; do not resubmit a new command to discover whether the old one ran. Check the real MCP tool list and a read-only filesystem request through the existing endpoint before accepting mutations.

For rollback, stop container admissions and the container, retain the current command directories, and restore the original native coding frontend launch configuration. Because the absolute paths and durable records are unchanged, the original Python implementation can continue the same jobs. LM Studio and native model-memory control are outside this handoff. This package has not changed any live launchd service.
