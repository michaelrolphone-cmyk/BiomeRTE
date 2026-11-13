# SDR migration qualification

Verified on the Raspberry Pi through BiomeRTE's radio.sdr.receive:1 capability.

- Native Python 3.4 daemon/client test passed: separate client processes, receipt deduplication, changed-input rejection, event retention and error envelopes.
- sdr-devices-live-3 succeeded: four RTL2838 receivers, indexes 0-3. All report serial 00000001, so exact-serial selection is ambiguous.
- sdr-sample-device1 and sdr-results-device1 succeeded at 433.92 MHz: 131072 complex samples, 201 spectrum bins, 32768 bytes of cu8 IQ.
- Receiver index 0's earlier scan timed out. Index 1 is the qualified receiver; this does not establish that every receiver is healthy.
- sdr-monitor-migrate-live preserved the existing seven frequencies and measurement settings, replacing missing serial stx:1090:37 with index 1, monitor revision 6.
- sdr-status-live-3 returned successful 262144-sample observations for all seven frequencies: 300, 310, 315, 318, 372.5, 390 and 433.92 MHz.
- sdr-retire-legacy30 removed the old local service through the legacy host's revision-checked remove operation: removed=true, quiescent=true, legacy_sdr_present=false. Logs, receipts, package cache and shared radio state were retained.
- Legacy Channel Bus route python-service:participant_bbacda11-6cc3-490c-a31a-e009c8ff5037:linux.pi-sdr.receive was explicitly withdrawn at revision 3 (operation sdr-route-retire-20261113-r2). Historical generation routes were already expired or withdrawn.
- Both qualification leases were released. The persistent SDR daemon and provider remain enabled.

Gateway calls intermittently returned transport errors after accepting commands. Results were reconciled with cap.status and identical request IDs; no uncertain hardware operation was blindly replayed.
