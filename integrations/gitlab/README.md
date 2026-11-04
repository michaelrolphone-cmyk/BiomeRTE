# GitLab on Mac and Biome Git capabilities

The provider discovers GitLab's native MCP tools and preserves their input schemas in the catalog and capability usage. Each is exposed as git.gitlab.<tool>:1. git.mcp.tools:1 and git.mcp.call:1 provide generic discovery and dispatch. Native git.cli:1 and git.clone/fetch/pull/push/status/diff/log/commit/checkout/branch/merge/add/init:1 operate on namespaced worktrees on the Mac.

Acquire a lease, wait for bound, invoke with a stable operation ID and inputs {arguments:{...}}, read cap.status, inspect result.ok, then release. The existing Biome worker retains fences and never blindly repeats uncertain effects. MCP protocol/transport failures leave an uncertain command requiring reconciliation. Tool-level failures return ok=false. Large JSON results use SHA-256-verified artifact chunks.

Credentials stay in owner-only files on the Mac. GitLab has separate persistent config/data/log volumes and a pinned native ARM64 image. Native Git uses a credential helper with the local TLS CA; credentials are never embedded in clone URLs. Worktrees are serialized by repository across concurrent leases. Commands have no artificial execution timeout.
