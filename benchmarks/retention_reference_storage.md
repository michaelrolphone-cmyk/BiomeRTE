# Retention reference projection storage qualification

Synthetic qualification on the development executor, not measurements of the Pi
or Mac. No existing controller database was opened or changed.

The fixture has 60,000 nested canonical records, 209,700,000 JSON bytes, 64-character
identifiers, repeated field names, arbitrary ordinary strings, eight nested cross-record
references per record, and opaque output strings. This intentionally exercises
metadata-heavy reference storage, not just large output blobs. All 2,820,000
reference edges survive the format change.

| Metric | Text projection v2 | Compact projection v3 |
| --- | ---: | ---: |
| Canonical database pages | 61,546 | 61,546 |
| Additional projection pages | 240,069 | 19,454 |
| Additional projection bytes | 983,322,624 | 79,683,584 |
| Additional projection MiB | 937.77 | 75.99 |
| Total database pages | 301,615 | 81,000 |
| Reference edges | 2,820,000 | 2,820,000 |

Pages are 4,096 bytes. The compact projection reduces allocated projection storage
by **91.90%**, from 4.69 times the raw JSON size to 0.38 times its size. Canonical
storage is unchanged. Full object/page breakdowns are in `results/`.

The compact representation stores kind/ID strings once per source, maps each
128-bit SHA-256 prefix to an integer token, and stores integer-only source/token
pairs in a WITHOUT ROWID table plus its reverse index. The fixture has 120,037
unique tokens. Hash collisions only retain additional records; they cannot remove
an actual reference. Orphan tokens are reclaimed transactionally when their last
edge is removed. Unknown/durable roots, transitive reachability, dirty tracking,
partial progress and fail-closed readiness remain in effect. Old projection
formats rebuild incrementally without modifying canonical records.

Requirements: production SQLite >= 3.8.2 (WITHOUT ROWID), Python >= 3.4. Confirm
actual target runtime before deployment. The benchmark additionally uses SQLite's
optional `dbstat` table for measurement only; production does not require it.

The measurements used 64-row/131,072-character indexing ticks with commits. Raw
reports include build and per-tick timings, but those are executor measurements,
not target latency qualifications. The compact storage run preceded the final
keyset optimization, which replaces a prefix-filtering query with two indexed
seeks; that optimization does not change edge contents or schema. WAL checkpoint
I/O is included in timing, and the initial bulk fixture transaction temporarily
uses a large WAL. Compare persistent allocated pages, not temporary WAL files.

Reproduce on a new file from the repository root:

    PYTHONPATH=. python benchmarks/retention_reference_storage.py /tmp/new-reference-benchmark.sqlite

Run the same script against the v2 checkout for the baseline. The script refuses
to open an existing output path. The default fixture is 200 MiB/60,000 records;
`--mib` and `--rows` can select a smaller smoke test. Do not run on a live database.
