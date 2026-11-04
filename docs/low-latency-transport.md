# Persistent transport and combined capability requests

`cap.submit` accepts `contract`, `resource`, `operation` and `inputs`. It durably
queues admission and dispatch in one transaction. A physical provider must still
acknowledge the original fence before claiming work. Capacity admission,
replication and provider result validation use the existing controller paths.
`cap.submission` returns admission or action state. Terminal success/failure
releases the lease; uncertainty retains the fence for explicit reconciliation.
Reusing an operation with changed inputs is rejected. A disconnected client is
not required to advance a queued command.

Android uses an OAuth-authenticated `POST /api/exchange` with `contract`,
`resource` and the original encrypted `envelope`. Its NDJSON response sends
heartbeats and the final status. A transport interruption preserves the original
pending state in the encrypted phone vault. Resume sends the same envelope,
never a fresh inference. The old MCP exchange remains as a fallback for gateways
that return 404/405 before accepting work. Fresh cryptographic handshakes,
identity pins, encrypted history, image artifacts and model selection remain.

Mac providers multiplex existing MCP operations over `/api/provider/connect`.
An empty provider inbox read stays open server-side; heartbeat frames keep the
connection live. OAuth is validated on connection and messages, and during held
inbox reads. Expired/revoked credentials reconnect through existing authorization.
The same immutable RPC ID is retained for HTTP fallback after connection loss.

Controller links use `/bridge/connect`, authenticated by the existing dedicated
device credential. The gateway pushes leased jobs and accepts their completion
on the same socket. The durable D1 job queue remains authoritative across socket
loss. Existing priority, overflow, capacity, heartbeat and failover selection
remain in `claimControllerJob`; completion must match controller, job and lease.
No routing authority is stored exclusively in an isolate or socket.

Install the optional pure-Python transport dependency beside each module:

```
python3 -m pip install --target deploy/transport_vendor -r deploy/transport-requirements.txt
python3 -m pip install --target providers/mac-terminal/transport_vendor -r providers/mac-terminal/transport-requirements.txt
```

The pinned controller-compatible version supports the existing Pi Python 3.4.
Vendor directories are deployment dependencies and must not contain credentials.
If the optional package or connection is unavailable, existing HTTP calls remain
operational. Command execution has no new lifetime or CPU limit; socket liveness
and acknowledgement waits affect transport recovery only.

Validation covers queued restart recovery, fence enforcement, replay conflicts,
terminal release, retained uncertain fences, authenticated NDJSON delivery, live
status on resume, and Android completion identity without client-side polling.
