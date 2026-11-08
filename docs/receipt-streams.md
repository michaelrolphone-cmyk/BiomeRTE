# Bounded mutation receipt streams

This is an opt-in protocol for future mutations. It does not migrate or delete legacy receipts, deployment history, journal entries, user records, or artifacts. Deploying this code alone does not reduce the existing journal or change existing clients' storage behavior.

## Wire contract

In the existing command envelope, replace the string `id` with an object:

    {"stream": 0, "sequence": 1}

The object has exactly these two keys. `stream` is an integer from 0 through 15. `sequence` is an integer from 1 through 9223372036854775807. Booleans are not integers for this protocol. The existing authenticated envelope signs the complete ID object along with operation and arguments. Slots are scoped by authenticated actor and realm. Local admin clients share an actor and must coordinate slot ownership.

Each slot starts at sequence 1. A new mutation must be the next consecutive sequence. Retrying the latest committed sequence with identical operation and arguments returns the retained result. Changed operation or arguments return 409. Earlier sequences return 410 and never run again. Gaps return 409 and never run. Failed transactions do not advance the sequence. Permission checks apply before replay as before.

Advancing to the next sequence acknowledges that the caller no longer needs the preceding receipt result. Clients must durably persist their pending operation, arguments, slot and sequence before sending, retry the exact envelope after an uncertain result, and persist the successful result before advancing. Concurrent outstanding requests need separate owned slots. Never reset or reuse a slot's sequence on restart. A lost local sequence requires explicit reconciliation, not sending guessed new mutation IDs. There is no slot reset or automatic actor rotation. At the sequence maximum the stream stops accepting new mutations.

The latest result is retained only if its canonical JSON UTF-8 representation fits in 65,536 bytes. Larger results are returned to the original caller normally, but only the signature and committed sequence remain in replay state. Retrying such a committed request returns 410 with `Request committed; result exceeds replay byte limit`, without invoking it. Callers opting in must be able to recover large results through the domain's authoritative read operation or accept unavailable replay. They must never interpret 410 as permission to submit the same effect under a new sequence. No external-effect compensation or replay is attempted.

## Bound and authority

At most 16 `_receipt_stream` records are retained per actor per realm, with at most 1 MiB of aggregate result payload plus small fixed metadata. Replacing one record rolls off its previous response copy. The sequence floor and current receipt are authoritative deduplication state, not disposable debug logs. Their lifetime is the actor's entire request lifetime. Deleting these records permits duplicate execution and is unsafe.

Legacy string request IDs retain the original `_receipt` semantics. Their receipts are not expired. Arbitrary legacy IDs cannot safely be compacted without a separately established lifetime or replay fence. Read-only operations continue to create no receipts. The total domain database is not bounded by this protocol: actor count, legacy clients, retained user data, pending work, and journal/archive history remain separate growth sources.

## Integration and rollout prerequisites

1. Review and integrate only `biomerte/api.py`, this document, and `tests/test_receipt_streams.py` from the isolated branch. The source baseline is controller commit `61ab6000df840178af97339c038a3a37bff4c32d`.
2. Upgrade every writer serving opted-in clients before client migration. Old binaries reject dictionary IDs, rather than silently interpreting them as legacy strings, but cannot serve these clients. Restore old binaries only after stopping opted-in clients. Do not rewrite IDs into legacy strings in a proxy or fallback path.
3. Migrate selected controller/gateway callers only after implementing durable sequence and slot ownership plus the explicit result-recovery contract above. No SDK change is included here. Existing direct Python/HTTP Application calls pass the ID object through and authenticated calls sign it unchanged. Other connectors and authorization wrappers may validate IDs as strings and must be separately qualified before opting in.
4. Checkpoint creation/restoration must retain every `_receipt_stream` record along with `_receipt` and all other authoritative current state. Test recovery through a fresh mirror, lost append acknowledgement, and failed/concurrent CAS. This change uses ordinary transactional record writes; it adds no special store recovery path.
5. Bounded shared-journal/archive storage still needs verified checkpoint restore and separately authorized segment reclamation. Replacing a current record adds another journal entry; it does not reclaim historical bytes. Do not claim Pi/Mac disk growth is fully remediated by this protocol alone.

No existing history is modified and no deployment is performed by this change.

## Transactional refresh operations

The operation registry also supports `receipt='refresh'` for operations whose contract is to read current state while updating liveness, without creating a new domain identity or irreversible effect. `bus.discover` exposes `receipt` as `replay`, `refresh`, or `none` for read-only operations.

`cap.lease` adopts this policy end to end for existing clients: new legacy string IDs no longer create a `_receipt`; calls still run in a writable transaction and update the reservation heartbeat. Repeating a new ID returns current lease state, not a frozen prior response. Existing stored legacy receipts are checked first and remain replayable with their original argument-conflict checks. Explicit dictionary stream IDs retain ordered-stream semantics even for refresh operations. No SDK or caller modification is needed for string-ID polling to stop accumulating redundant lease response copies.

Only `cap.lease` is classified as refresh. `cap.offer` changes availability and emits events; provider claims/results affect work execution. They retain normal receipts. The policy is an explicit operation contract, never inferred from a method name or from an example. Refresh classification is not a claim that the operation is read-only, and must not bypass authorization or journal replication. Heartbeat updates still generate journal entries until checkpoint-based reclamation handles that separate storage layer.

## Future receipt payload retirement

New legacy replay receipts include `format: 2`, `operation`, and `created_at`. A separate retention policy may retire an eligible response payload by removing `result` and adding `result_expired: true`, while retaining the signature and metadata. Replaying that request returns 410 and never executes again; conflicting arguments still return 409. This patch does not itself decide which domain receipts are safe to retire. Historical receipts without format 2 stay untouched. This bounds disposable payloads only when an integrated retention policy enforces a bound; small per-request identity fences remain required state.

## Worker-local terminal result copies

After `provider.result` returns a response matching the exact operation, provider, generation, terminal state, and result, the worker atomically retains its action identity/signature/state fence, marks the result acknowledged, and removes the full payload from the authoritative action row. A separate recent debug table retains at most 100 result copies and 1 MiB of encoded result payload. Individually oversized responses are not copied. Uncertain and unacknowledged results remain intact. This applies only to actions created with a matching result_retention_eligible marker under this version. Action creation and eligibility are committed together before adapter execution. Legacy action payloads remain intact even after successful re-delivery, and unmarked cleanup intents are never executed. This is not a startup sweep of old history. A repeated acknowledged action cannot invoke the adapter again or submit the cleared payload.

An offer may configure `acknowledge_argv`, an absolute executable argument list for idempotent local adapter cleanup. The worker durably stores a cleanup intent alongside the acknowledgement before invoking it with JSON `{operation, result_digest}` on stdin. Failure leaves cleanup pending, and normal worker polling retries pending cleanup independently of action execution. The hook must tolerate duplicates, including concurrent/restarted cleanup attempts. It must remove only data identified as redundant by the verified acknowledgement. Hook stdout/stderr are discarded to avoid creating another unbounded log. Absent hook configuration preserves existing adapter behavior.

Newly created, eligible reconciliation audit copies also compact after exact
acknowledgement, retaining byte digests and provenance plus a separate bounded
recent-debug window. Existing audit rows remain untouched. See
[worker persistence coverage](worker-persistence-retention.md) for the complete
worker-owned table/file inventory, recovery behavior and restart-check contract.
