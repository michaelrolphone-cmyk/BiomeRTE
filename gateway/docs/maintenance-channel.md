# Independent Mac maintenance

For Raspberry Pi and other Linux hosts, see the additive
[Linux maintenance channel](linux-maintenance-channel.md).

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

## Repair an already enrolled Mac

Run `gateway/recovery/Repair-Biome-Maintenance.command` as the enrolled Mac user.
This local bootstrap does not enroll or revoke a host, use the Decisions app,
import the normal controller, or depend on its command queue. It reads the Python
interpreter and home from the existing `org.biomerte.maintenance` LaunchAgent.
A missing enrollment, unsupported service configuration, wrong interpreter or
unloaded service produces an explicit failure without replacing the agent.

The script carries the exact standalone agent and its SHA256 pin. It checks and
compiles that payload, preserves a private copy of the previous agent, replaces
only `agent.py` atomically, and runs `launchctl kickstart -k` for that existing
maintenance service. It preserves the plist, configuration, device credential,
SQLite ledger and command output directories. It does not restart another service.
Existing running ledger rows retain the agent's documented restart behavior:
they become uncertain and are not executed again. Other recorded IDs are also
never replayed.

A successful script reports the preserved previous-agent path and the requested
service restart. Verify actual recovery with a fresh owner-authenticated
maintenance command and its completed result; service restart alone is not
completion evidence. If restart fails after replacement, the verified agent and
previous-agent backup remain available and the script reports failure.

The installer and repair payloads must match `maintenance_agent.py` byte for byte.
Offline tests cover payload parity, durable directory/thread-start failure
results, duplicate/uncertain execution fences, preserved enrollment and ledger,
atomic replacement failure and fixed-label service restart. They simulate
launchd and command processes and run under the existing Linux/macOS CI discovery:

    python3 -m unittest tests.test_maintenance_recovery -v

Local service recovery: `launchctl kickstart -k gui/$(id -u)/org.biomerte.maintenance`.
Local removal: boot out that service, remove its LaunchAgent plist, and revoke the
host through `maintenance.revoke`. Preserve its ledger until uncertain commands
are reconciled. Do not remove the normal controller or terminal services.
