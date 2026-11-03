# Mac Docker provider

This login agent adds a Mac Docker resource to the existing `docker.cli:1` contract. It uses the installed BiomeRTE Mac terminal v3 gateway client and its locked OAuth refresh path. The Docker worker has its own provider ID, fencing ledger and pool. The default is four concurrent CLI commands against Docker Desktop's local Unix socket. No Docker TCP listener is needed.

From a persistent checkout with the Pi Docker adapter and pooled worker, run this as the enrolled Mac user in a logged-in macOS session:

```sh
python3 providers/mac-docker/mac_docker.py install
```

The LaunchAgent starts at user login. It opens Docker Desktop when the local engine is unavailable and advertises the offer as ready once the engine version endpoint responds. For an independently managed engine, use `--external-engine --socket /absolute/path/to/docker.sock`. Set the CLI path with `--executable` and concurrent leases with `--slots`.

Discover `docker.cli:1`, select the `docker.mac.…` resource and supply it in `cap.acquire`. Once bound, invoke with a unique operation ID and the native Docker arguments, for example `{"argv":["info","--format","{{.Name}} {{.ServerVersion}} {{.Architecture}}"]}`. Read the command through `cap.status`, check `result.exit_code`, then release the lease. The Pi and Mac have independent image, volume and container stores. The [Docker CLI guide](../pi-docker/README.md) describes standard streams, host paths and artifact retrieval.

Use `launchctl print "gui/$(id -u)/org.biomerte.mac-docker"` to inspect the agent. The manifest, SQLite ledger and worker logs are under `~/.local/share/biome-mac-docker`. To stop it, run `launchctl bootout "gui/$(id -u)/org.biomerte.mac-docker"`.
