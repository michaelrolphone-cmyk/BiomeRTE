# GitLab on Mac and Biome Git capabilities

The provider discovers GitLab's native MCP tools and preserves their input schemas in the catalog and capability usage. Each is exposed as git.gitlab.<tool>:1. git.mcp.tools:1 and git.mcp.call:1 provide generic discovery and dispatch. Native git.cli:1 and git.clone/fetch/pull/push/status/diff/log/commit/checkout/branch/merge/add/init:1 operate on namespaced worktrees on the Mac.

Acquire a lease, wait for bound, invoke with a stable operation ID and inputs {arguments:{...}}, read cap.status, inspect result.ok, then release. The existing Biome worker retains fences and never blindly repeats uncertain effects. MCP protocol/transport failures leave an uncertain command requiring reconciliation. Tool-level failures return ok=false. Large JSON results use SHA-256-verified artifact chunks.

Credentials stay in owner-only files on the Mac. GitLab has separate persistent config/data/log volumes and a pinned native ARM64 image. Native Git uses a credential helper with the local TLS CA; credentials are never embedded in clone URLs. Worktrees are serialized by repository across concurrent leases. Commands have no artificial execution timeout.

## Deployed service

GitLab 19.5.3 EE is running without a paid subscription on the native ARM64 image pinned in compose.yaml. HTTPS is https://Micahels-Mac-mini.local:8443 and SSH uses port 2424. This is a local-network service; coding agents outside the LAN use Biome to reach the Mac. The self-signed certificate is verified by the proxy and Git helper; import the public certificate before using the web interface on another device.

The live catalog contains 37 GitLab MCP tools and 54 Biome contracts in total. The native catalog is the authority for input schemas; it is not a hand-maintained approximation of a newer GitLab release. Repository creation uses git.gitlab.api:1 because this deployed MCP catalog does not include a project-creation tool.

A dedicated nonadministrator biome-agent account owns agent-created projects. Give it membership in other projects agents should access. The proxy uses GitLab OAuth with mcp/api/read_repository/write_repository scopes and refresh-token rotation. Local owner-only files hold credentials. The access ZIP contains the separate root login and public certificate, never the service OAuth secret.

## Agent usage

The existing coding MCP HTTP endpoint retains its filesystem and terminal tools and adds git_tools, git_start and git_status.

1. Discover names and schemas with git_tools.
2. Call git_start with contract and arguments.
3. Retain its operation ID and call git_status until settled.
4. Check result.ok; a succeeded worker lifecycle alone does not establish tool success.

Example project creation arguments for git.gitlab.api:1:

```json
{"method":"POST","path":"/api/v4/projects","body":{"name":"Example","path":"example","visibility":"private","initialize_with_readme":true}}
```

Example clone arguments for git.clone:1:

```json
{"namespace":"example","path":"repo","url":"https://Micahels-Mac-mini.local:8443/biome-agent/example.git"}
```

Native aliases use options arrays, for example git.checkout:1 with {"namespace":"example","path":"repo","options":["-b","feature/example"]}. git.cli:1 takes an argv array. Worktrees are inside the existing coding filesystem workspace at git/<namespace>/<path>, so file tools and Git share the same files.

For direct Biome callers, acquire the contract, wait for the lease to bind, cap.invoke with inputs {"arguments":{...}}, retain the operation ID, read cap.status, and release the lease after settlement. Uncertain effects require inspection using their original operation; never blindly repeat commits, pushes, or merge-request creation.

## Operations and recovery

Host state is under ~/.local/share/biome-gitlab. GitLab data/config/logs use the three external Docker volumes named in compose.yaml. Keep gitlab.rb, tls/, GitLab's gitlab-secrets.json, database and repositories together in backups. A volume is persistence, not an off-host backup.

The container uses restart=unless-stopped. The provider LaunchAgent org.biomerte.gitlab-provider starts at user login and retains a durable worker ledger. Container restart and user-login startup are separate from a full hardware reboot.

install.py refreshes the catalog, preserves existing credentials, installs the pinned repository worker runtime, and extends the existing coding facade while backing up its original source. bootstrap.rb is initial provisioning only: it creates a new OAuth application/token and must not be rerun for normal updates. OAuth refresh is automatic; an interrupted refresh is explicitly unresolved rather than replayed blindly.

Run python3 integrations/gitlab/test_proxy.py for eight automated tests, including real local Git init/add/commit/log/clone, namespace traversal, MCP pagination/error handling, API validation, and SHA-256 chunk reconstruction. Docker tests install Git in a disposable build/test container.

qualify.py exercises the actual authenticated coding MCP HTTP endpoint through Biome: private project creation, clone, branch, commit, push, merge request, MCP project read and Git log. It preserves operation IDs and receipts under ~/.local/share/biome-gitlab/qualification.json. Re-running reads the same operations; it does not create duplicate test repositories or merge requests.

The initial live qualification passed against biome-agent/biome-git-qualification, with commit d93188a on biome-proxy-test and merge request !1. The repository is intentionally retained as concrete evidence.

The org.biomerte.gitlab-server login LaunchAgent starts Docker Desktop if needed, waits for the engine, and starts the existing biome-gitlab container. It was launched and verified against the running engine. A full Mac power cycle remains untested. It does not read or modify Docker Desktop's protected settings file.
