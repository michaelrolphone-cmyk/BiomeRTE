# Independent Mac maintenance

This temporary channel repairs Biome when the controller, capability registry,
workflow engine, or normal command queue cannot service requests. It shares the
hosted OAuth gateway and its database, but uses separate inbox tables and an
independent Mac launch agent. It does not provide access during a Mac power loss
or a hosted gateway/database outage.

## Authenticated command admission

Commands submitted through the existing owner-authenticated MCP connection start
in `queued` and do not require a separate signed phone approval. The gateway
continues enforcing owner/OAuth authentication, host enrollment and revocation,
and the independent device bearer credential over TLS. The agent still records
command IDs before execution, preventing duplicate execution.

Existing pending, rejected, cancelled and uncertain commands are not automatically
released or replayed by this change. Legacy signed decision records remain readable
and retain their original settlement rules. Other decision processing is unchanged.

## Operator use through the existing authenticated MCP connection

- `maintenance.status`: inspect agent heartbeat and command states.
- `maintenance.submit`: submit `{host:"mac-maintenance",argv:["/bin/bash","-c","..."]}`.
  The caller's unique `request_id` is the durable command ID. Optional `cwd`
  specifies the working directory. The command queues immediately for the enrolled host.
- `maintenance.result`: read `{id:"the-command-request-id"}`.
- `maintenance.cancel`: owner-authenticated cancellation of `{id:"the-command-request-id",reason:"..."}` before approval, or after a non-authorizing written reply. It preserves the command and audit history and cannot cancel approved or executing work.
- `maintenance.revoke`: disable `{host:"mac-maintenance"}`.

All are passed to `biome_invoke`. These operations do not enter `bridge_jobs` or
the capability queue. The signed phone inbox and response use the existing
`/api/decisions` endpoint, with maintenance signature verification independent of
the controller. The independent device inbox is `/maintenance/device`.

## Mac installation and recovery

Service: `org.biomerte.maintenance`, installed as a user LaunchAgent with RunAtLoad
and KeepAlive. Files: `~/.local/share/biomerte-maintenance/`. Its dedicated random
credential is stored locally with mode 0600; the gateway stores only its hash.
The agent uses Python's standard library and does not import BiomeRTE modules.

The gateway retains active command requests, signed approval evidence and results.
New terminal operational payloads use bounded prospective retention; exact replay
fences remain after expiry. See [maintenance payload retention](maintenance-result-retention.md).
The Mac retains a SQLite execution ledger and per-command stdout/stderr files.
Duplicate deliveries do not re-execute a command. An agent restart marks unfinished
commands uncertain; inspect the recorded PID and output files before requesting
another execution. No automatic replay of uncertain work is performed.

Commands have no software execution deadline or CPU-time cap. Output is retained
without software truncation; hosting, filesystem and memory capacity remain real
physical constraints. Maintenance is not a substitute for normal workflow dispatch.

Local service recovery: `launchctl kickstart -k gui/$(id -u)/org.biomerte.maintenance`.
Local removal: boot out that service, remove its LaunchAgent plist, and revoke the
host through `maintenance.revoke`. Preserve its ledger until uncertain commands
are reconciled. Do not remove the normal controller or terminal services.
