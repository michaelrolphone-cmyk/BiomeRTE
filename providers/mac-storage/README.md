# Mac workspace and sacred Pi backup

| Storage | BiomeRTE interface | Placement |
| --- | --- | --- |
| Sacred primary | Existing Pi `storage.file.*:1` resource | Authoritative 2 GiB volume, asynchronously copied to the Mac |
| Shared workspace | `storage.namespace.create:1`, the eleven `storage.file.*:2` operations, `storage.namespace.remove:1`, `storage.workspace.capacity:1` | 98 GiB fixed local Mac image. Unreplicated. Available while the Mac, its mounted image and provider are online |
| Sacred backup | `storage.sacred.status:1` and the local recovery command | Separate, physically reserved 2 GiB Mac archive with verified generations |

The allocation uses GiB, matching the Pi volume. Filesystem overhead reduces usable workspace bytes. The capacity operation reports reserved, usable and available bytes. Lease capacity in discovery counts simultaneous calls.

## Use the workspace

Create a namespace with `{"namespace":"build-cache"}`. Open `{"namespace":"build-cache","path":"/example.bin","mode":"x+"}`, then pass that namespace and the returned handle to write, read and close. The read/write payload format is the same base64 representation as the Pi service. Paths, listing, handles, renames and the namespace root are scoped to one namespace. Traversal is rejected. Only an empty namespace can be removed. Namespaces separate project file trees; enrolled operators can deliberately select the same namespace to share it.

Acquire the selected contract and resource, wait for a bound lease, invoke with a unique operation ID and read `cap.status`. Check `result.ok` and `result.result` or `result.error`. Release the lease. Each offer includes an example. The file survives provider restarts and ordinary shutdowns but has no off-Mac replica.

## Replication and recovery

Every five minutes while the Mac login service is running, a pinned SSH connection pulls from a forced, read-only Pi command. The Pi checks the mount and volume UUID and snapshots its rollback-journal SQLite database under a read transaction. It releases the database before network transfer. The Mac reuses matching blocks from the previous copy, verifies changed blocks and the full SHA-256, checks SQLite integrity and foreign keys, and verifies the published archive candidate by restoring its latest database. It atomically replaces the archive only after these checks. An interrupted or rejected update leaves the last verified generation available. A daily full comparison verifies even an apparently unchanged source.

The archive retains the newest snapshot and up to 23 older distinct generations that fit. Failed syncs are retried by the login timer. Status reports the last successful check, last verification, timestamps, digest and generation history. A new write is protected on the Mac after a successful pull. Downtime lengthens that interval. SQLite snapshotting needs temporary space on the Pi and Mac. Neither temporary space nor the backup archive is exposed through the shared workspace.

From the Mac, recovery works while the Pi and controller are offline:

```sh
tool="$HOME/.local/share/biome-mac-storage/current/providers/mac-storage/backup.py"
python3 "$tool" list
python3 "$tool" restore "$HOME/restored-files.sqlite"
python3 "$tool" restore "$HOME/older-files.sqlite" --generation GENERATION_ID
```

Restore creates a new verified file and refuses an existing destination. Each generation is also a standard gzip-compressed SQLite file in `~/.local/share/biome-mac-storage/sacred-backups.tar`. Its `index.json` gives the archive member, original volume UUID and SHA-256. To return a restored copy to service, stop the Pi file provider, preserve its existing store, check the restored UUID against its manifest, and install the verified SQLite file onto the correctly mounted Pi volume before starting the provider. The Mac backup remains read-only to capability clients.

## Installation and dependencies

The Mac uses the enrolled terminal provider's OAuth transport and shared pooled worker. The Pi keeps its existing file provider. The controller must support versioned contracts, per-offer usage, capacity, and configuration. The Mac installer runs as the enrolled login user.

1. Install `source.py` as `~pi/.local/share/biomerte-file-storage/sacred-export.py` on the Pi. On the Mac create a private `~/.local/share/biome-mac-storage` and generate a dedicated Ed25519 key named `replica-key` there. Enroll its public key in the Pi account’s authorized keys, limited to `command="/usr/bin/python3 /home/pi/.local/share/biomerte-file-storage/sacred-export.py",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty`. The private key stays on the Mac.
2. Pin the Pi SSH host key independently in the Mac storage directory’s `known_hosts` with the alias `biome-sacred-pi`. Set `replica.json` to `{"volume":"PI_VOLUME_UUID","hosts":["PI_ADDRESS","ProximityRF.local"]}`. Use the source UUID from the Pi file-storage manifest. The Mac must be able to reach Pi SSH.
3. Keep this checkout under a persistent path, with `current` in the Mac storage directory pointing to it. Run `python3 providers/mac-storage/volumes.py` from an unrestricted Mac login shell or launchd job. It provisions the fixed image and reserved archive once and checks their identities on restart. Then run `python3 providers/mac-storage/provider.py install`. This installs the storage and five-minute backup LaunchAgents.
4. On the Pi install `primary.py` in the file-storage provider directory. Set its `replica-target.json` to `{"resource":"MAC_SACRED_RESOURCE"}`, using the advertised Mac status resource. Install the supplied systemd service and timer, reload systemd and enable/start the timer to maintain primary classification across legacy worker restarts.
