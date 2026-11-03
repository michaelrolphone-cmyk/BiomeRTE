# Raspberry Pi terminal provider

On a Pi with the Channel Bus Linux terminal worker and a running BiomeRTE controller, run this once as the account that owns both installations.

```sh
python3 providers/pi-terminal/install.py
```

The installer commissions a separate signing identity and enables `biomerte-terminal.service`. On startup that service advertises `host.terminal.run:1` and polls the controller over loopback. It uses the installed Pi terminal supervisor and the configured Bash digest and execution limits. Channel Bus connectivity is independent. The Pi account's ordinary privileges, including its configured sudo privileges, apply to commands. Grant terminal access only to trusted operators.

Use `cap.discover` with `{"contract":"host.terminal.run:1"}`, then `cap.acquire` with a distinct task ID for each independent session and the contract. Optionally pin the Pi's resource. The default is 16 independently fenced leases on that resource. Bash commands on different leases can overlap, and commands on each lease execute serially. Set `--slots N` on the installer to choose a different limit. This pool is separate from Channel Bus admission. If all slots are held, the request queues. Set `reply_to` for a durable grant notice; read `cap.lease` for current lease state. Wait for the Pi to bind the lease. Call `cap.invoke` with that lease, a stable operation ID and, for example, `{"argv":["/bin/bash","-c","hostname"]}` as the inputs. Read `cap.status` with the same operation ID and release the lease with `cap.release` when finished. Workflows can invoke the same contract through a service activity.

The result contains the command's exit code, the first 4096 bytes of each stream decoded as UTF-8, and execution evidence. For the exact output, concatenate the base64-decoded `artifact.get` results for each stream’s ordered `chunks` and verify the stream digest. Invalid argv returns exit 126 with `evidence.executed=false`. A command's own nonzero exit is preserved.

The service keeps its key, manifest, execution ledger, and per-operation logs in `~/.local/share/biomerte-terminal`. Reinstalling reuses this identity and refreshes the local terminal profile. The native worker fences leases and preserves uncertain effects for operator reconciliation. Use the runtime guide’s `resolve-effect` procedure with the provider home and manifest after inspecting the local job log. Service status and logs are available with `systemctl status biomerte-terminal` and `journalctl -u biomerte-terminal`.
