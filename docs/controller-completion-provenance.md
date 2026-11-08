# Controller completion delivery

HTTP inbox claims are not registered in a gateway socket's delivery map. Sending
their results over that socket previously waited for an impossible ACK for 15
seconds before using HTTP completion. The controller link now retains the exact
socket that delivered each socket job as local-only metadata. HTTP claims and
jobs from a previous connection complete over the existing authenticated HTTP
path immediately. Native request bytes, job IDs, leases, receipts, HA routing,
capacity and result framing are unchanged.

Socket ACKs are accepted only for the same connection, job ID and lease. A late
ACK for another lease cannot release a newer waiter. A legacy gateway's ID-only
ACK wakes immediate HTTP reconciliation without being treated as proof of that
lease's completion. If the legacy socket already committed the result, the HTTP
endpoint may return 409 and the existing link logs a retry; the durable public
result remains available. Echoing the lease in gateway ACKs is an optional,
backward-compatible optimization, not a prerequisite for deploying these files.

Qualification on the workspace used the original and fixed completion functions
with a silent socket and an in-memory HTTP receiver. The real original ACK wait
took 15.001397 seconds; the fixed HTTP-claimed path took 0.000020 seconds. Both
delivered exactly the original job ID, lease and result. This measures the
connector delay, not physical-host network latency.

Run `python3 -m unittest discover -s tests -v`. The dedicated
`test_controller_completion.py` regressions cover HTTP provenance, exact socket
provenance, reconnect, send failure, missing ACK, explicit rejection, legacy ACK,
late old-lease ACK, wrong-session ACK and overlapping waiters. Runtime files and
the focused test use Python 3.4-compatible APIs; qualification on the actual Pi
interpreter remains required before rollout.
