# Pi SDR provider

Standalone receive-only service migrated from Channel Bus's `radio.sdr.receive@1` / `sdr-receiver@1.0.1`.
The radio implementation and capture helper are preserved in main.py and capture.py.
No controller-core changes are required.

## Installation

Requires Linux, Python 3.4 or later, NumPy, librtlsdr, USB permission, an enrolled BiomeRTE controller, and systemd.
Use a pinned BiomeRTE runtime:
```sh
python3 install.py prepare --runtime /path/to/runtime --url http://127.0.0.1:8740 --site SITE
# Approve the returned fingerprint for provider operations at the controller.
python3 install.py activate --runtime /path/to/runtime --url http://127.0.0.1:8740 --site SITE
```
The installer creates a distinct provider identity, manifest, persistent SDR daemon and provider worker.
Existing state in ~/.local/share/sdr-receiver is retained. Ensure the legacy SDR service has stopped before starting this daemon.
The worker uses normal BiomeRTE leases, fencing and operation-result custody.
Provider placement belongs to the offer; callers normally omit placement.

## Calls

Discover `radio.sdr.receive:1`, acquire a bound lease, invoke with a unique operation ID, read cap.status, then release.
Alternatively use cap.submit for acquire/invoke/release.
Check the provider result's `ok` before reading `data`.

| action | input | result |
| --- | --- | --- |
| devices | none | indexes, USB serials, uniqueness, receive_only |
| scan / sample | frequency_hz or frequencies_hz; device, dwell_seconds, width_hz, gain_db, ppm, threshold_db, include_iq optional | scan_id |
| results | scan_id; offset optional | state, one window, next_offset |
| status | none | monitor revision/settings, latest observations, current scans |
| monitor_start | frequencies/settings and expected_revision from status | persisted monitor configuration |
| monitor_stop | expected_revision from status | persisted disabled configuration |
| events | since and limit optional | RF candidates, receiver errors and explicit observations |
| mark | label and optional note | explicit physical-test marker |

Each spectrum window includes mean/max-hold frequency bins, timing, device identity, sample count and relative dBFS.
include_iq returns a bounded base64 cu8 interleaved I/Q excerpt.
The legacy garage_us hopping profile remains available.
RF activity does not establish a decoded source identity or physical door position.
Scans remain asynchronous; the daemon retains their in-memory jobs across capability invocations.
As in the legacy service, restart clears transient scans; monitor configuration and latest summaries persist.
Receipts reject operation-ID reuse with different inputs. Reconnect never authorizes blind replay of uncertain commands.

## Verification

`python3 -m unittest discover -s providers/pi-sdr -p 'test_*.py'` exercises the real daemon/client lifecycle, result persistence across client processes, receipt identity, and error responses without RF capture.
Qualify USB discovery and actual spectrum/IQ capture separately through BiomeRTE.
