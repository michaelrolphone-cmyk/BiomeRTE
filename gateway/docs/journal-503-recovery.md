# Journal 503 investigation and local correction

## Scope and provenance

This candidate was first reproduced on committed source
`1d10f4519151894dbe9995f68b20bf182a0c0c53`, then ported to the verified deployed
v44 source `dd41356671234c37cb209857ffd44d986db67aaa`. Both defects and the
relevant recovery paths are unchanged in v44. This report precedes deployment.
No controller request was replayed, no relay was actuated, and no production
configuration, reservation, journal object, or service was changed.

## Production evidence, 2026-11-09 UTC

- Rover retained map read `rover-read-14ff9a41-89f8-4848-b3f3-d527be9b4053` had
  an initial controller-response timeout, then repeated `Gateway journal HTTP
  503` during recovery with its original submission request ID. At 10:48:30,
  the retained recovery attempt counter was 47.
- Read-only `cap.submission` and `cap.status` confirmed that both the submission
  and action records were absent. The connector classified these errors as
  INVALID_ARGUMENT; their underlying text was `Unknown _submissions ...` and
  `Unknown actions ...`.
- Production gateway logs contain 12 `/bridge/ha/append` HTTP 503 responses
  from 10:53:10 through 10:54:09, each paired with the message
  `Journal upload backlog full; retry after maintenance`. Example request:
  `956518ff0bbccc4c4acbd1b340b96aeb`, 10:54:09.423.
- The same 100-event sample includes 71 append 409s. Earlier samples showed
  93 and 97 append 409s. These are CAS conflicts, not themselves HTTP 503s.
- The upload sweep record was not locked (`until=0`); it reported 135 completed
  cycles and 2,718 removed objects. This does not establish current aggregate
  backlog size. No broad scan or cleanup was performed.

The first historical failure of the retained Rover operation has not been
correlated to a particular gateway request. The logs prove an active cause of
journal 503s, not the cause of every earlier gateway error.

## Defects reproduced in accessible source

1. Append uploaded before CAS. Already-stale proposals and duplicate successful
   retries allocated new pending upload intents. CAS losers waited the full
   600-second upload grace period. Admission rejected at 256 pending/collecting
   intents. A test with one winner and 20 stale proposals plus a duplicate
   produced 22 uploads before the correction and only one afterward.
2. The bridge cached transient journal errors as completed results. Identical
   retries kept returning the cached error until the one-hour bridge cleanup.
   They did not consult the controller's durable command receipt again.

## Candidate correction

- Resolve existing identical commits and already-stale proposals before upload.
- Keep the atomic append CAS authoritative for races after the preflight read.
- Following a definite CAS result, expire only this attempt's uncommitted
  managed upload intent. The committed pin is protected by the existing SQL
  trigger. Normal bounded collection performs cleanup; no inline object delete
  is added. Uncertain CAS outcomes retain their intent.
- On an identical explicit caller retry, requeue only the bridge attempt for an
  exact transient GatewayJournal error. Keep original request and operation IDs.
  Clear the old bridge claim, use CAS to deduplicate concurrent callers, retain
  the existing 40-request admission bound, and apply a two-second cooldown.
- Successful results, domain errors, different request bodies, unrelated tool
  calls, and arbitrary provider text remain unchanged.

## Before production use

The exact v44 source was retrieved through an authorized repository-scoped
Sites credential and the candidate was ported there. Run the full v44 suite
and build before publication. Publishing replaces a
production gateway worker and may reconnect long-lived transports, so it must
be coordinated around the user's active critical task. Do not raise the upload
limit, disable pinning, clear committed records, or replay device actions as a
substitute for the correction.
