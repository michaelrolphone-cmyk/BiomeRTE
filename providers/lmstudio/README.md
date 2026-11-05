# BiomeRTE LM Studio coding service

The current Mac mini coding-service source is maintained here on `main`, together
with its dependency lockfile and regression tests. This service does not modify
the separate `feature-change` workflow.

## Installed configuration

- LM Studio 0.4.28 build 2, running Qwen3.5-9B Q4_K_M (`qwen3.5-9b`).
- Model context: verified per loaded instance; text-only context exhaustion can expand it up to the model's supported context.
- Agent workflow: `agent.lmstudio`; capability: `agent.lmstudio.delegate:1`.
- Coding workspace: `/Users/micahelbyrns/Developer/biome-lmstudio` on local disk.
- Service files: `~/.local/share/biome-lmstudio`.
- LM Studio API: authenticated `http://127.0.0.1:1234`.
- Coding MCP: authenticated `http://127.0.0.1:48731/mcp`.
- Filesystem MCP dependency: official `@modelcontextprotocol/server-filesystem`
  2.1.0, installed with a lockfile under `coding-deps`.

The agent consumes signed, target-filtered MCP Events, verifies the authoritative
delegation, claims it, runs the model, completes the delegation, and acknowledges
the inbox event. Both active controller event modules need the `target` filter
support. The consolidated controller preserves that filter together with HA,
without changing default ChatGPT/Grok routing. Its regression test checks both
the named LM Studio receiver and the default ChatGPT receiver.

For coding, LM Studio's OpenAI-compatible tool-calling endpoint chooses tools.
The adapter executes them through the local MCP server. The official filesystem
server provides reads, searches, edits, writes, directory operations, and moves.
Two added tools, `start_command` and `command_status`, execute builds, tests, and
Git commands through BiomeRTE's durable Mac terminal capability.

This route was selected after LM Studio's native configured plugins timed out
during initialization and its per-request MCP API rejected local addresses.
The service does not depend on either of those failing integration routes.

## Delegating work

Acquire `agent.lmstudio.delegate:1`, wait for a bound lease, and invoke with:

```json
{
  "request": "Inspect the project, implement the requested change, and run its build and tests. Do not commit or push.",
  "context": {"coding": true, "workspace": "project-directory"}
}
```

The workspace directory must already exist under the configured coding root.
Read `cap.status` using the same operation ID until settled, then release the
lease. Results include the final text, actual tool call results, model identity,
workspace, and a local response ID. Pass that ID as `context.previous_response_id`
with a review request to continue the conversation in the same workspace.
`context.coding: false` retains the original structured, text-only route.

## Service operation and recovery

The per-user launchd jobs are `org.biomerte.agent.lmstudio` and
`org.biomerte.lmstudio-coding-mcp`. They start at user login and restart after a
process exit. LM Studio's authenticated server is set to start on application
launch. A cold machine still requires the user's macOS login and LM Studio to
be available; this is not a pre-login system daemon.

`install-lmstudio-agent.py` updates an already configured installation. It checks
coding prerequisites, copies the source files, and reloads the two services.
Run it with the configured Mac Python interpreter. Services use Python `-S`;
their Python dependencies are all standard library modules plus the existing
`mac_provider.py` transport. The HTTP server uses Homebrew Node.

Private `api-token`, `mcp-token`, OAuth files, databases, and actual machine
configuration are deliberately excluded from the repository. Preserve the
existing private files when updating. The LM Studio application rollback copy is
under `~/.local/share/biome-lmstudio/update-0.4.28`.

Logs are `service.log`, `service.err`, and `coding-mcp.log`. Inference checkpoints
are in `runs`; durable shell operation records are in `commands`. If a model or
tool response is lost after work may have executed, the agent fails closed rather
than replaying edits or commands. Inspect the workspace and recorded operation
IDs before submitting a new delegation. A terminal command is only successful
when its actual result has exit code zero; queue acceptance is not completion.

Filesystem tools enforce the configured directory root. Shell commands run with
the Mac user's permissions; checking their initial working directory is not an
OS sandbox. No automatic Git commit, push, or workflow publication is configured.

## Verification

Run the adapter regression suite with:

```sh
python3 -S -m unittest discover -s . -p 'test_coding.py' -v
```

The adapter tests use isolated fixtures and do not call the live model or edit a
real coding workspace. A live model repair/build/test remains separate hardware
qualification; preserving its source does not rerun that exercise.

To restore the exact filesystem MCP dependencies, copy `coding-deps/package.json`
and `coding-deps/package-lock.json` to the installation's `coding-deps` directory
and run `npm ci` there. The installer updates an already configured installation;
keep its existing workspace configuration, tokens, OAuth identity and ledgers.

## Context exhaustion recovery

The ownership review on 2026-11-04 stopped after a 9,961-token prompt filled
its 16,384-token loaded window (server n_tokens=16383, truncated=1), despite
max_tokens=-1. Output-token limits and the model's loaded context are separate.

Text-only requests now run through context_recovery.py. The existing shared
ModelRuntime admission covers discovery, unloading/reloading, every prediction,
and verification, so image and other model tasks remain serialized. Deployments
without that optional installed coordinator keep the standalone loading path.
The requested task model is preserved; a response from another model is rejected.

A length stop is recoverable only when returned usage reaches the verified
context boundary (or the native stop reason confirms context exhaustion), output
is uncapped, and no tool calls are involved. Recovery grows the loaded window,
verifies the model and capacity, and reruns the unchanged pure request. Growth
ends at the model's advertised maximum or an actual load failure. There is no
fixed retry count or generation deadline. Missing usage, a smaller output cap,
transport uncertainty, coding tool calls and ignored resize requests fail
explicitly; partial JSON is never accepted as a complete answer.

Verified capacities are remembered per model in owner-only context-lengths.json
so eviction for another workload does not restore the known insufficient window.
The file contains model identifiers and capacities only. Logs/results include
usage, context length and finish reason, without prompt/response bodies. Coding
requests also explicitly disable a saved output-token cap while retaining their
checkpoint and no-replay semantics.

Run both adapter and recovery tests with python3 -S -m unittest discover -s
providers/lmstudio -p 'test_*.py' -v. Native full-workflow qualification is separate
from unit-test success; record its actual result in the work item.
