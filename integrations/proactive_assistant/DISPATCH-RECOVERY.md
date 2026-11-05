# Dispatch failure recovery

The controller's process engine can complete a run whose business outcome is
faulted or unresolved. Engine completion must not be presented as assistant
success.

A dispatch allocates an episode ID before an episode exists. Failed dispatch
reconciliation checks the actual episode records and matching task reservation.
It releases a reservation only when there is no episode and no unresolved or
external submitted effect. A failed external command is not proof of no effects.
Epoch and cycle checks prevent an old dispatch from releasing a replacement.

Failed dispatches are recorded in assistant_dispatch_failures. Planning skips
those tasks while continuing independent work. There is no timed retry loop,
retry count, or new execution timeout. After correcting the cause, inspect
assistant.status(id), then call assistant.retry(id, task_id, failure_run_id) with
a unique request ID. The failure run must match and the previous reservation
must be safely released. Retry retains normal governance and Android
authorization; it does not grant approval or replay uncertain operations.

On startup/tick, terminal legacy dispatch rows are reconciled once, including
rows previously labeled completed. assistant.status reports business disposition,
result code, active runs and blocked dispatches separately from engine state.

Reasoning requests send the workflow JSON schema directly to LM Studio, retaining
the existing provider result envelope and actual model identity. Text inference
explicitly sets max_tokens=-1 to remove LM Studio's saved prediction-length cap;
the model's real context window still applies. Truncated answers remain failures.
Coding tool execution behavior is unchanged. Pending requests are polled using
their frozen original action before considering a new context budget.

Qualification:
- CI runs the complete assistant suite on Linux and macOS, plus runtime, provider,
  gateway and replicated recovery suites.
- Live qualification must deploy both controller support and the LM Studio
  provider, run a structured reasoning request, then retry the recorded failed
  dispatch and observe an actual reasoning episode.
- An unclaimed Mac terminal request blocks deployment and live qualification;
  repository tests alone do not establish live recovery.
