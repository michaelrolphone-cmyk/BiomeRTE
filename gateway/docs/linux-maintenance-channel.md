# Independent Linux and Raspberry Pi maintenance

This additive channel keeps owner-authenticated diagnostic access outside the
BiomeRTE controller, capability queue and controller database. It supports the
Pi's Python 3.4.2 and systemd 215. It does not edit controller source, restart the
controller, move its data, or change the existing Mac agent.

## Durable execution and storage stalls

The transport process loads the device credential and verified TLS context once.
Its periodic heartbeat reads only procfs and network-interface ioctls. The gateway
retains the latest authenticated snapshot: boot identity, uptime, IPv4 and MAC
addresses, route table, physical block counters, memory and executor state.
Read it through owner-authenticated maintenance.status.

A separate executor process owns ledger.sqlite and command stdout/stderr files
on the persistent local filesystem. SQLite uses WAL and synchronous=FULL. It
commits each immutable command identity before creating an execution thread or
subprocess. Duplicate delivery never repeats execution. Restarted unfinished
commands become uncertain. Full results remain in the durable ledger until the
gateway acknowledges the exact host, command ID and canonical result digest.

The parent does not wait for SQLite startup, admission, output-file writes or
completion commits. If those operations block, heartbeat and read-only diagnosis
remain available. It stops fetching additional executable jobs until the worker
can accept them. It never bypasses durable admission to run a repair command.

The /run directory contains only the single-instance lock and disposable runtime
scratch. The ledger and command output are never placed on tmpfs or network
filesystems. The interpreter, loaded code, TLS roots and credential stay in the
warm process; this avoids routine SD reads and writes from diagnostic polling.
Interpreter/library pages can still fault from the SD card, and commands can
require SD-backed executables. The channel cannot overcome kernel failure, lost
power, failed Wi-Fi, a gateway outage, or an unreadable persistent configuration
at boot. A heartbeat is diagnostic reachability, not proof that mutations can
commit or that the controller is healthy.

There are no command execution deadlines or software output truncation.
Transport socket timeouts permit reconnection without cancelling any command.
Service stdout/stderr go to null so repetitive connectivity failures do not
create an SD-backed journal. Command output is retained separately and durably.

## Preparation and secure enrollment

Review and test the exact source revision before use. Run preparation as the Pi
service user, using the agent and installer from the same reviewed revision:

    /usr/bin/python3 -B -S gateway/recovery/install_linux_maintenance.py \
      --home /home/pi/.local/share/biomerte-maintenance \
      --origin https://biomerte-controller.cantikash.chatgpt.site \
      --host pi-maintenance

Preparation writes a private 32-byte random device credential encoded as 64 hex
characters, configuration, the agent source and a reviewable service unit.
Existing different files are refused, not replaced. It prints only the SHA-256
credential verifier, source checksum, paths and activated:false. The raw
credential stays on the Pi with mode 0600; do not put it in command arguments,
chat, logs, source control or the Mac's configuration.

Through the existing owner-authenticated gateway, invoke maintenance.enroll with
a fresh request_id and these arguments:

    {"host":"pi-maintenance","key_hash":"<printed SHA-256 verifier>","agent":"linux-v1"}

No controller selector is used. Linux enrollment is authorized by the existing
gateway owner session and does not call the controller or require a retired
phone approval. It cannot change an existing host identity or reuse another
host's credential. Revoked identities remain revoked; replacement uses a new
host and device credential.

Verify enrollment and real certificate/hostname validation before activation:

    /usr/bin/python3 -B -S \
      /home/pi/.local/share/biomerte-maintenance/linux_maintenance_agent.py \
      /home/pi/.local/share/biomerte-maintenance --probe

This posts only a read-only diagnostic heartbeat and checks the returned host.
The Python 3.4.2 transport explicitly creates a verifying SSL context and
HTTPSHandler; it does not rely on that interpreter's unsafe urlopen defaults.
HTTP redirects and implicit proxy configuration are rejected. If the Pi's CA
store cannot validate the gateway, update the trusted CA material through the
reviewed installation path; never disable certificate or hostname verification.

## Additive service activation

The installer never calls systemctl. Review its generated unit, install only
that unit, and then explicitly enable and start the new service:

    sudo install -o root -g root -m 0644 \
      /home/pi/.local/share/biomerte-maintenance/biomerte-maintenance.service \
      /etc/systemd/system/biomerte-maintenance.service
    sudo systemctl daemon-reload
    sudo systemctl enable biomerte-maintenance.service
    sudo systemctl start biomerte-maintenance.service

Check maintenance.status for a fresh heartbeat and executor:ready. Submit one
harmless command through maintenance.submit with a unique request_id, then
recover its matching maintenance.result. Verify restart reconciliation and the
independent diagnostic path in tests before any disruptive infrastructure work.
A unit merely being active or a probe-only heartbeat does not prove command
custody works.

To remove or upgrade the channel, first reconcile every running or uncertain
maintenance command. Stopping the service stops its process group, so do not
stop it to test independence while repairs are executing. Preserve the ledger
and output spool. Revoke only the dedicated Pi maintenance identity when removal
is intended; leave the normal controller and terminal services alone.

## Qualification

Run the new Python regression suite on a normal development host and on the
actual Pi Python 3.4 interpreter, using a writable tmpfs directory for disposable
test fixtures if available. Test fixtures are isolated and are not a production
replay ledger:

    python3 -B -S -m unittest discover -s gateway/recovery \
      -p 'test_linux_maintenance_agent.py' -v

Run the gateway protocol tests with the repository's supported Node version:

    node --test gateway/tests/maintenance-linux.test.mjs \
      gateway/tests/maintenance-completion.test.mjs \
      gateway/tests/maintenance-requests.test.mjs

Coverage includes actual valid/untrusted/wrong-host TLS connections, rejected
credential redirects, immutable command identity, commit-before-spawn, failed
commit admission, restart uncertainty, exact completion ACKs and lost ACK retry,
diagnosis while the executor is blocked, private non-activating preparation,
tmpfs/network-ledger rejection, independent owner enrollment and unchanged
legacy Mac delivery. The old Mac approval-gate test is unrelated to this agent
and is not used as evidence for Linux compatibility.
