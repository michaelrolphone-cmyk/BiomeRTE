# Independent slot and RPC reply progress

A live WebSocket does not establish that each multiplexed RPC has a reply. The Mac terminal transport now returns an unanswered control RPC to its existing HTTP fallback after the existing 90-second HTTP response horizon. It retains the identical durable request ID and keeps unrelated socket calls connected. This does not cancel or impose a deadline on terminal jobs. Late replies cannot create a second logical operation.

Fence reconciliation now runs in the worker's bounded pool under the same per-guard serialization as actions. A slow reconciliation on one slot no longer blocks the shared poller or admission and execution on another slot. A pending fence always precedes an action on its own guard. The exact-identity and explicit-quiescence requirements remain unchanged; old uncertain effects are not cleared by this liveness change.

Regression checks reproduce missing replies, independent reply progress, original-ID fallback, independent action/fence progress, and same-guard ordering through successful reconciliation. The prior implementation fails the missing-reply and slow-fence tests. Combined qualification includes the final SQLite transaction fix from 16a207b. The patched complete suite passes 194 tests (one optional test skipped). Shared worker syntax remains Python 3.4 compatible.

Live Mac source hashes were compared before deployment. Worker and persistent transport match the tested baseline. Separate controller SQLite lock contention and older uncertain terminal receipts require their own fixes or supported evidence-based reconciliation; this patch does not claim to resolve those safety holds.
