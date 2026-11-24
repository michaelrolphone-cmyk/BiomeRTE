# Mac terminal provider

The macOS installer packages the repository’s `biomerte.worker` and the Pi provider’s terminal offer, argv validation and artifact writer. Both hosts expose `host.terminal.run:1`, default to 16 slots and execute one command at a time on each lease. The controller handles queued leases and sends `lease.available` when the provider fences the promoted lease. The Mac-specific pieces handle macOS subprocesses, launchd and outbound OAuth gateway transport.

Build the standalone installer from a checkout:

```sh
python3 providers/mac-terminal/build_installer.py install-biome-mac-terminal-v4.sh
```

Run the resulting script as the enrolled user on macOS. It preserves the node identity, resource, OAuth session and SQLite execution ledger. Use separate leases for concurrent work. To wait for capacity, acquire with `enqueue: true` and `reply_to` set to the caller’s address. Read command results through `cap.status` and full output through the referenced `artifact.get` chunks.

The live controller-side qualification `tests/remote_terminal_pool_qualification.py CONTROLLER_HOME RESOURCE` verifies 16 overlapping processes, a queued seventeenth lease, its notification and byte-exact output on the promoted lease.


Terminal jobs and their adapter have no default execution deadline. The Mac reads optional process limits from `~/.local/share/biome-mac-terminal/terminal-limits.json` before each job and inherits the host's limits when the file is absent. Changes take effect on the next command. The 128 MiB memory ceiling is specific to the Raspberry Pi. [Policy fields and decision record](../pi-terminal/README.md#command-lifetime-and-resource-policy)
# Headless provider authentication

The provider runtime can authenticate directly with a private signing key. It
signs each request locally and verifies enrollment through the existing public
native endpoint. Provider registrations, worker databases, leases, and stream
attachments keep their existing IDs. The key is scoped to that provider and its
existing offers. Operator and browser sessions use their existing sign-in.

An operator can provision a running host through its enrolled maintenance
channel. Use the installed `mac_provider.py prepare-machine-key PROVIDER` to
generate a private key on the Mac and obtain its public key. Read the provider's
registered resource and contract IDs. Call `peer.bind_provider_key` as an
operator with the exact provider ID, public key, resources, and contracts. Run
`mac_provider.py activate-machine-key PROVIDER ACTOR` on the Mac with the
returned key fingerprint. Activation requires an authenticated live proof and
atomically switches only that provider. A worker using the updated transport
picks up the selection on its next provider call. Run `auth-status PROVIDER` to
verify it independently of the worker inbox.

The provider signs again on reconnect using the same private key. An interrupted
reply retries the same signed request and operation ID. The machine transport
never refreshes OAuth. Its private status file distinguishes authentication
failure, an unavailable key, and gateway transport failure. A lost or revoked
key can be replaced over the maintenance channel by enrolling a new key. Revoke
the old key with `peer.revoke_provider_key` and its fingerprint once the
replacement is verified. Do not change the provider identity or reset its ledger.
