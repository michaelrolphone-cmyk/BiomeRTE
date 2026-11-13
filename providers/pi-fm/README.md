# Pi FM broadcast provider

Publishes the provider-independent contract `radio.fm.transmit:1` through BiomeRTE.
No Channel Bus or legacy service host is used. The persistent daemon owns the
transmitter context, so a job survives completion of its capability invocation.

Actions: `capabilities`, `transmit`, `status`, `stop`. Transmit requires explicit
`frequency_hz` (88100000..107900000) and `duration_seconds` (1..120).
Use `source=tone` with optional `tone_hz` and `volume`, or `source=wav` with
an absolute local PCM16 WAV `audio_path` under the execution user's home.
Volume controls audio modulation, not RF power.

The response is `{ok,data}` or `{ok:false,error}`. A transmit response returns
`data.job_id`; poll status until terminal and inspect `cleanup.verified`.
Stop requires that job ID. Use a new Biome operation ID for each logical call;
repeat it only to recover the identical call. Optional `request_id` supplies
the durable broadcast identity; otherwise SHA256(Biome operation ID) is used.
No broadcast is automatically retried or resumed. The transplanted privileged
watchdog retains the backend digest check, exclusive clock lock, parent-process
identity, duration deadline, explicit stop marker and clock cleanup.

## Installation

The source backend files main.py, common.py, worker.py and hardware.py were
preserved from the inspected fm-transmitter 1.0.0 package. common.py pins the
installed transmitter binary by absolute path and SHA256. Changing that backend
requires inspection and qualification. Run tests with:
`python3 -m unittest -v test_adapter test_watch test_wav` from this directory.

Define the compact contract in contract.json on the target controller.
Initialize a separate provider home using the runtime's `biome init --site-id`
and begin `biome pair URL --label "Pi FM broadcast provider"`.
Approve only the required operations plus BOTH the contract and physical-resource
grants. For host ProximityRF the resource is radio.fm.gpio4.proximityrf:

- cap.offer, cap.fenced, provider.poll, provider.claim, provider.result
- provider.custody, cap.heartbeat, cap.command.state, cap.status, artifact.put
- offer:radio.fm.transmit:1
- resource:radio.fm.gpio4.proximityrf

The installer completes pairing and installs biomerte-fm.service and
biomerte-fm-provider.service:
`python3 install.py --runtime RUNTIME --site SITE --url URL --home PROVIDER_HOME`.

The worker advertises its actual execution hostname and physical resource.
Callers normally select only the contract. Multiple command slots permit status
and stop requests; the backend serializes physical transmission and returns busy
rather than launching another broadcast.

## Legacy retirement

Qualify the native capability before retirement. Withdraw the exact legacy
radio.fm.transmit offer and routes, remove only its startup profile, and retain
historical contracts/receipts. Do not stop the shared service host or other
providers. This deployment found the old offer already withdrawn, no ready
legacy FM routes and no loaded service profiles; the retired package is archived and a retirement marker records
the replacement. The new daemon reuses the historical local job state without
resuming it.

WAV conversion waits for the bounded input/output conversion to finish; the inherited
15-second wall-clock cutoff was removed after a live qualification failure.
The emission duration and privileged watchdog deadlines are unchanged.
