# Prepared motion handoff

The native preparation command writes a private, concrete Compose bundle without
starting or stopping any worker. It uses the locally qualified immutable image ID,
preserves the actual provider identity and capacity, and binds the existing provider
ledger and observation/training database directories at their original absolute paths.
No database file is copied. The existing production HostIO TLS8766 client files are
referenced directly; a private observer bearer token is derived from the same native
controller secret already used by the installed observer.

## Current prepared bundle

`/Users/micahelbyrns/.local/share/biomerte-worker-containers/motion-90cc2f6-20261120`

The bundle contains `compose.json`, `provider.json`, `observer.json`,
`rover.token`, the nonsecret receipt and preflight records, and exact native
rollback plist/manifest copies under `rollback/`. Files are private to UID501.
The image is:

`sha256:3bb07e94f55d8ddaf1ff2457d727df79050618b1e9e3d6ebc09761324acd335c`

The checked-in `preparation-20261120.json` records the actual preparation and
runtime gate. It is evidence of preparation, not a claim that a live cutover occurred.
The raw endpoint checks are retained in the private bundle. A successful transport
request does not establish fresh motion observations: cached snapshots, manifest
messages, raw JPEGs and control readiness must be distinguished.

## Generate another bundle

Run the helper on the native Mac only while the databases still have native
ownership. The destination must be new; existing bundles are never overwritten.

```sh
python deploy/workers/motion/prepare.py \
  --native-manifest /Users/micahelbyrns/.local/share/biome-rover-motion/provider/provider.json \
  --controller-config /Users/micahelbyrns/.local/share/biomerte-controller/node.json \
  --client-directory /Users/micahelbyrns/.local/share/biomerte-worker-containers/client \
  --launch-agents /Users/micahelbyrns/Library/LaunchAgents \
  --output-directory /Users/micahelbyrns/.local/share/biomerte-worker-containers/motion-new-bundle \
  --rover-origin http://host.docker.internal:8093 \
  --provider-host Micahels-Mac-mini.local \
  --home /Users/micahelbyrns --uid 501 --gid 20
```

The helper validates the local image ID and source revision, the HostIO TLS endpoint
and mount paths, state ownership, the native launchd labels, and the rendered
Compose configuration. Its SQLite connections use query-only, read-only
transactions and close before inspecting open files. It never instantiates the
native controller or refreshes credentials. The ledger snapshot is a preparation
observation; it must be checked again immediately before a handoff.

## Paired handoff gates

The deployment owner must record intent and establish these conditions before
changing service ownership:

1. Confirm the native HostIO listener is stable. From the qualified image, verify
   the authenticated clock, coherent vision snapshot, sonar, native vision stream
   and existing Rover control subscription. Send no drive/LED/training commands.
   Check actual capture age and control readiness, not only HTTP success. Confirm
   the gateway can discover the actual motion contracts and investigate missing
   provider advertisements.
2. Recheck the provider ledger, including active actions, supervised pending
   commands, reconciliation and result recovery. Preserve completed, unacknowledged
   results in the same ledger. Check active or draining training sessions and
   account for their custody before stopping the observer.
3. Recheck native database consumers. The inspected installed Rover service uses
   capability calls and its separate pending-routes database; it does not open the
   motion database. Native vision motion evidence and ToF input use in-memory/JSON
   sources. A point-in-time lsof result alone does not establish future exclusivity.
4. Disable both native launchd labels so login/reboot cannot create a second owner,
   then stop both services. Verify the recorded PIDs have exited, no equivalent
   process has restarted, and no native process has either motion database/WAL/SHM
   open. Keep the complete current state directories intact. macOS and the Docker
   VM do not enforce each other's SQLite/fcntl locks.
5. Only after exclusivity is established, start the two services together:

```sh
/Applications/Docker.app/Contents/Resources/bin/docker compose \
  -f /Users/micahelbyrns/.local/share/biomerte-worker-containers/motion-90cc2f6-20261120/compose.json \
  up -d --no-build --pull never
```

Both services use the same Linux kernel for SQLite coordination, preserve the
provider ID/site/resource and UID501:20, and receive a 60-second graceful stop period.
The prepared Compose file has `pull_policy: never`; it does not require a registry.

Verify the running image IDs, stable process ownership, all seven motion contract
offers, current recorder heartbeat and accepted sensor/control evidence, and
read-only model/training queries. Perform database checks from inside the container
after ownership moves; do not open the live SQLite files from native Python.
Do not start a training session or command physical motion solely to validate
deployment. Report source degradation explicitly if it prevents a functional pass.

## Guarded rollback

Stop both container services and verify they are gone before restoring native
ownership. Confirm no invocation remains executing and no Linux process retains
the database. Preserve the current provider ledger, model, named sessions and
database/WAL files, including any results written since cutover. Never restore an
older database snapshot over current custody state.

Re-enable and bootstrap the two original native labels using the exact saved
plists under the bundle's `rollback/` directory. Verify their actual original
programs/PIDs and the restored native observer/provider behavior. The saved native
manifest is for comparison/recovery; preparation does not modify the original
manifest. Do not copy it over unrelated newer configuration.

No handoff or rollback is performed by `prepare.py`.
