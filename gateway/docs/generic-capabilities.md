# Generic capability map and placement

The public contracts and offers discovery sections now aggregate the registered controllers. Each versioned contract appears once, with its implementations and their actual provider/resource/controller metadata. Explicit controller selectors remain available for diagnostics. Offline source inventories and conflicting contract definitions stay explicit in coverage; partial discovery never establishes global absence.

Call cap.acquire, cap.submit, cap.prepare or cap.run_async with a contract and normal arguments. Omit resource and placement to let the gateway resolve an eligible implementation. Optional placement fields provider, host and resource constrain this request only. For example, host.terminal.run:1 can ping Google on any eligible terminal or read /proc/meminfo with placement.host equal to the advertised Pi hostname. Host names are execution metadata, never inferred from IDs. A specific workspace or attached device requires resource affinity.

Current provider placement excludes stale replicated offers. Capacity observation can select an available implementation on another controller. The gateway resolves a concrete resource/provider internally, so callers do not bind their capability identity to a machine. Concurrent retries retain that resolution; changed request payloads cannot reuse the same request ID. Expiring selection records contain only a request digest and the current resolution, not command inputs or execution history.

Native lease fences, local command runners and completion receipts remain authoritative. When a gateway command locator is missing, native completion evidence is checked before any command can be dispatched again. Ambiguous custody, changed inputs or an unreachable potential owner fail before execution. Continue from the returned owner-bound next_call; no connection replaces another consumer.

Catalog pagination retains per-controller read snapshots and reports source coverage. It is not an atomic snapshot across all machines. Native admission and fencing still decide when a selected implementation can execute. Software checks do not establish physical robot behavior.
