# Receiver selection and antenna coverage

The generic `radio.sdr.receive:2` (with the original version 1 kept for compatible callers) capability includes RTL-SDR and explicitly commissioned ESP-SDR receivers. Existing numeric RTL indexes and unique serial selectors remain supported. Omit `device` (or use `auto`) to select by frequency.

Selection requires the **whole requested window**, center ± width/2, inside the receiver's effective bands. Auto selection prefers configured antenna coverage, then the narrowest matching band, then higher receiver_priority, then stable receiver ID. No candidate returns an error; it never silently samples an unsupported frequency. Mixed-frequency jobs preflight every window and pin selected physical receiver IDs and configuration revision.

## Coverage configuration

`devices` or `receivers` returns all receivers, availability, stable ID, aliases, hardware bands, antenna bands, effective bands, and a revision. Use `receiver_configure` with that revision:

```json
{"action":"receiver_configure","device":"rtl:usb:1-1.5.2","expected_revision":1,"antenna_ranges_hz":[[430000000,440000000]],"receiver_label":"433 MHz antenna"}
```

The ID above is an example; use actual inventory IDs. Settings persist in receivers.json. RTL IDs identify physical USB ports because several dongles share the serial 00000001. Keep each antenna/receiver in its configured port; moving ports requires reassigning coverage. Reordering numeric device indexes does not transfer antenna settings.

- Unconfigured antenna bands (null/omitted) use hardware coverage, without claiming known antenna performance.
- An empty list excludes the receiver from automatic selection, appropriate for an unattached antenna.
- Explicit selection still permits capture outside antenna coverage with an antenna_warning, within hardware limits.
- receiver_enabled=false disables both manual and automatic use.
- Stale expected_revision is rejected. Reset antenna coverage with null (direct adapter) or action receiver_reset_antenna with device and expected_revision.

The unattached RTL antenna has not been physically identified; no dongle is arbitrarily excluded.

## ESP-SDR

Install/commission with explicit `--esp-serial 28:84:85:4B:A1:1C --esp-alias a11c`. Other connected ESP boards are never probed. The registered ID is `esp:2884854ba11c`; `device:"a11c"` explicitly selects it. By default 2.4 GHz requests automatically use it.

RTL hardware policy is 25 MHz–1.75 GHz. The ESP32-S3 policy is approximately 1.85–2.75 GHz, based on the firmware project's measured S3 reception span, with local commissioning at 2.442 GHz. This is not a calibrated guarantee of sensitivity across the entire interval. Firmware RANGE 100..6000 only accepts tuning attempts and does not establish working reception or PLL lock. Narrow effective antenna bands as hardware is characterized.

ESP acquisition uses 16 MS/s, minimum characterized 13 MHz analog bandwidth, signed 8-bit burst IQ, CRC verification, and corrects the firmware (I,-Q) convention. Output IQ remains cu8 I,Q. The LO rounds to whole MHz and actual tuner_center_hz is reported; FFT bins retain their actual frequencies. Spectra use 4096-point Hann transforms (3906.25 Hz bins).

Dwell is a **wall observation budget with gaps** for ESP bursts; sample_seconds reports actual acquired RF time, not continuous observation. Hardware/transport preparation is separate overhead. No controller protocol decoding or source attribution is implied by spectrum activity. Relative dBFS is uncalibrated, not dBm.

ESP gain_index is a PHY index 0..82, not dB; omit for hardware AGC. Explicit gain_db or nonzero ppm with ESP is rejected. The ESP firmware's startup/background calibration can transmit even though this service never requests intentional transmission.

Reference: ESPARGOS/esp-sdr docs/rx-controls.md at b1e65189a900542ff18358095e01506b3e513afc.
