# Resident request diagnostics

The optional `deploy/controller_trace.py` launcher observes the existing controller. It retains the selected BiomeRTE runtime, home, operations, leases, durability and HTTP behavior.

Use the same interpreter, working directory and PYTHONPATH as the service being diagnosed. Replace only its launcher:

```sh
python3 -S /reviewed-release/deploy/controller_trace.py \
  --diagnostic-log /existing-controller-home/request-diagnostics.log \
  --home /existing-controller-home serve
```

The destination directory must already exist and be private to the controller owner. Restore the original launcher after diagnosis. Restart through the existing service manager; do not start a second controller against the same home.

Every second, the observer samples calls or ticks that have taken at least one second. That threshold only controls diagnostic collection. It never cancels, delays, expires or retries an operation.

Records contain an observation timestamp, operation name, thread identifier, elapsed time and phase: SQLite connection establishment, operation execution or commit/close. Pending records include up to 24 file/line/function frames. Request IDs, arguments, signatures, tokens, record contents, frame locals and exception messages are excluded.

The request path uses a nonblocking queue of at most 64 records. On overflow, diagnostic records are discarded; commands and results are unaffected. A separate observer drains the queue into a private mode-0600 rotating file, capped at 1 MiB plus one backup. Storage errors do not change command custody.

A stalled phase is evidence about where execution is waiting. It does not by itself establish the cause of an OS or SQLite lock. Keep debugger-attached intervals separate from natural latency acceptance. Preserve synchronous=FULL, current authority checks and fresh WAL snapshots while testing a repair.

Focused tests cover real read/write behavior, exception preservation, secret omission, bounded nonblocking queues and private rotation.
