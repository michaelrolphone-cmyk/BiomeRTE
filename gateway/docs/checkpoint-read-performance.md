# Bounded recovery read performance

Local-only candidate. No deployment, activation, publication, production database access or repeated live restore.

## Observed evidence and limits

The prior bounded live test stopped safely at its 600-second alarm. Its last progress sample was 100 transport calls and 9,631,669 received bytes at 450.75 seconds, not final totals. Sample gateway logs showed one checkpoint/objects response at 21,244 ms and two at 410 and 420 ms (HTTP 200). Those logs do not distinguish D1, R2 get/body, hashing, transport or native staging time. They are insufficient to attribute all delay to R2 or predict a new live completion time.

Source inspection establishes two independent serialized operations: native traversal separately starts each of 256 root buckets, and gateway pages sequentially await per-object retention lookup, R2 retrieval, bounded decode and digest verification.

## Gateway change

readCheckpointObjects uses fixed waves of at most four complete object reads. Existing lease renewal precedes all reads. Each read still checks site-scoped retention, bounded object size, digest and content hash. Results and failures are consumed in request order. All wave promises settle before returning/throwing, with no detached reads. Output remains the same maximal prefix under the existing 2 MiB encoded-data cap and next cursor.

At a byte cutoff, at most three additional lookahead reads beyond the old serial boundary check are performed. Every request remains at most 128 objects. Retained encoded result data is at most 2 MiB plus one four-object wave (each <=512 KiB); transient decoding/JS object overhead is additional. Corruption beyond an already-reached page boundary is ignored until requested on the next page, matching serial behavior. This is not concurrent native HTTP and introduces no credentials, pin changes or writes other than the existing renewal.

Synthetic benchmark: 128 1-KiB objects, each D1 lookup 3ms + R2 get 8ms + body 4ms. Serial 2,048ms; candidate 518ms (3.95x). Both perform 128 D1 and 128 R2 calls. Max concurrent R2 goes from one to four. These are simulated latencies, not a live ETA.

## Native companion patch

GatewayJournal traverses root descriptors together through the existing bounded page iterator rather than restarting it per bucket. It preserves hash, prefix placement, next-cursor, response-order, record-size and aggregate page-byte checks. Prefix association uses ordered positions rather than a digest-keyed dictionary, preserving duplicate empty descriptor roots correctly. Traversal remains depth-first, with fixed depth <=64 and root count 256; pending descriptors are digest/prefix metadata, never full values. Only one HTTP request remains outstanding.

Existing 77,000-record disk-backed restore benchmark:
- Before: 1,114 object-page requests; max page 36,143 bytes; peak RSS 22,656 KiB; local elapsed 3.65s.
- After: 636 requests (42.9% fewer); max page 253,128 bytes; peak RSS 23,296 KiB; local elapsed 4.33s.
- Both verified 77,000 canonical records and position 77,000. No-network timing does not show CPU improvement; expected benefit is avoided remote round trips.

No restore proof for the full live head or historical prefix is claimed. Later live qualification requires parent coordination and the same bounded authorization envelope.
