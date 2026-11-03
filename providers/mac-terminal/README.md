# Mac terminal provider

The macOS installer packages the repository’s `biomerte.worker` and the Pi provider’s terminal offer, argv validation and artifact writer. Both hosts expose `host.terminal.run:1`, default to 16 slots and execute one command at a time on each lease. The controller handles queued leases and sends `lease.available` when the provider fences the promoted lease. The Mac-specific pieces handle macOS subprocesses, launchd and outbound OAuth gateway transport.

Build the standalone installer from a checkout:

```sh
python3 providers/mac-terminal/build_installer.py install-biome-mac-terminal-v4.sh
```

Run the resulting script as the enrolled user on macOS. It preserves the node identity, resource, OAuth session and SQLite execution ledger. Use separate leases for concurrent work. To wait for capacity, acquire with `enqueue: true` and `reply_to` set to the caller’s address. Read command results through `cap.status` and full output through the referenced `artifact.get` chunks.

The live controller-side qualification `tests/remote_terminal_pool_qualification.py CONTROLLER_HOME RESOURCE` verifies 16 overlapping processes, a queued seventeenth lease, its notification and byte-exact output on the promoted lease.
