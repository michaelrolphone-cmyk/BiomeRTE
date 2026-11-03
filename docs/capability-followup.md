# Run a capability and resume the calling agent

Start `capability-followup` with `biome_invoke`, operation `cap.run_async`. The launch returns a durable `run_id` immediately. The Biome controller advances the executable BPMN, waits for capacity and the provider’s final result, releases the work slot, selects one prompt, and invokes the caller’s registered agent capability. The launching agent can yield immediately. An identical retry must reuse the original MCP request ID.

## Launch

```json
{
  "operation": "cap.run_async",
  "realm": "site",
  "request_id": "build-347-launch",
  "arguments": {
    "contract": "host.terminal.run:1",
    "resource": "terminal.mac.ae3c74c06d1909a1ccdc",
    "arguments": {"argv": ["/bin/bash", "-c", "set -e; uname -s; hostname"]},
    "caller": "agent.chatgpt.delegate:1",
    "context": {"task_id": "build-347", "next_step": "record the build host"},
    "success_prompt": "Use the command output to record the build host and resume the task.",
    "failure_prompt": "Inspect stderr and the exit code, then decide how to recover."
  }
}
```

| Field | Meaning |
|---|---|
| `contract`, `arguments` | Registered work capability and its native input object. Discover the offer’s usage before launching. For a terminal sequence, pass one Bash script, using `set -e` or explicit exit handling when earlier failures should stop the sequence. |
| `resource` | Optional exact resource ID. Select the Pi or Mac offer for a host-specific command. Omit for any compatible resource. |
| `caller`, `caller_resource` | Calling agent’s registered capability contract and optional exact instance resource. Supply the resource when the originating instance matters. |
| `context` | Optional opaque correlation object, up to 8 KiB serialized. Supply the original task, conversation, checkpoint or parent run ID that the receiving agent needs to resume work. |
| `success_prompt`, `failure_prompt` | Caller-written prompts, up to 8,000 characters each. |
| `success_equals` | Optional object of dotted result paths and expected JSON values. All must match. For example, `{"result.verified":true}`. With no override, a successful provider response must also satisfy any returned `exit_code=0`, `ok=true`, `executed=true` and `timed_out=false` fields. An explicit empty object uses provider status alone. |

The workflow adds no execution deadline. Each provider still enforces its advertised command duration, file-size, and output limits. Choose a capability whose own job lifetime covers the requested operation. A queued invocation stays pending. An uncertain effect stays pending for reconciliation so it cannot trigger a false failure or a blind replay.

## Receive the continuation

The callback uses the agent capability’s normal input shape:

```json
{
  "request": "<selected prompt>",
  "context": {
    "caller_context": {"task_id": "build-347", "next_step": "record the build host"},
    "outcome": {
      "run_id": "<launch run_id>",
      "activity": "work",
      "realm": "site",
      "contract": "host.terminal.run:1",
      "resource": "<actual resource>",
      "provider": "<actual provider>",
      "operation_id": "<command ID>",
      "state": "succeeded",
      "provider_state": "succeeded",
      "ok": true,
      "error": "",
      "result_inline": true,
      "result": {"exit_code": 0, "stdout": "...", "stderr": "...", "streams": {}, "evidence": {}},
      "result_ref": {"operation": "cap.status", "realm": "site", "arguments": {"operation": "<command ID>"}}
    }
  }
}
```

The callback includes full structured output up to 16 KiB serialized. Larger results include a bounded preview and the authoritative `cap.status` reference. Terminal stream artifacts remain retrievable through their chunk references. A dispatch rejection before a command exists carries `state=dispatch_failed`, the error, and a null operation ID. A provider failure carries its original diagnostic payload.

ChatGPT’s agent contract uses its MCP event subscription. Grok’s uses its registered webhook. A polling provider such as Muse claims work from its normal inbox. A new agent participates by offering a capability that accepts `request` (string) and `context` (object). Transport selection stays in the registered agent adapter. Correlate by run and operation ID across notification retries. Callback work is held until the receiver completes it, and a receiver failure does not relaunch the original work.

The published BPMN can also be started with `process.start` using the same fields under `inputs` and `name: "capability-followup"`. Read `process.get` with the run ID for diagnostics or the final outcome and continuation reply. The executable source is [capability-followup.bpmn](../examples/capability-followup.bpmn).
