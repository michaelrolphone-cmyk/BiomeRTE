# BiomeRTE

BiomeRTE is a portable controller for work, executable processes, capability providers and offline collaboration. The same checkout runs on a Raspberry Pi, a Mac mini and a Linux server. The standalone runtime uses Python 3, SQLite and the OpenSSL executable. There are no pip packages, frontend builds or CPU-specific application binaries.

## Run a fresh clone

```sh
git clone https://github.com/michaelrolphone-cmyk/BiomeRTE.git
cd BiomeRTE
./biome init
./biome token
./biome serve
```

Open **http://127.0.0.1:8740** and paste the token. Keep that token private. On a headless machine, forward its loopback port over SSH from the computer with the browser:

```sh
ssh -N -L 8740:127.0.0.1:8740 user@controller
```

In another terminal on the controller, run `./biome demo`. It creates a goal, milestone, workflow and task, runs a versioned routine through a provider, saves a knowledge revision, and accepts the task with that revision as evidence. The command prints the actual IDs and final states. The browser also has a **Run a complete example** button that leaves the task in review for human acceptance.

Each clone creates its own identity and operator token in `~/.local/share/biomerte`. Use `BIOME_HOME` or `./biome --home /absolute/path ...` to change the data location. Copy or clone the source to another host and run `init` there. Copying an existing private node directory duplicates its identity and should be reserved for controlled recovery.

The source targets Python 3.4.2 or later, including older 32-bit ARM Linux. For a current Mac or cloud image, install Python 3, Git and the OpenSSL CLI if missing. Check with `python3 --version`, `openssl version` and `python3 -c 'import sqlite3, ssl'`. You can select another interpreter with `PYTHON=/absolute/path/to/python3 ./biome serve`.

## Keep the controller running

| Host | Command, from the checkout |
| --- | --- |
| Linux with a user systemd manager | `python3 deploy/install-service.py` |
| Pi or server using system-wide systemd | `python3 deploy/install-service.py --system` |
| macOS user session | `python3 deploy/install-service.py` |

Run the installer as the account that owns the node. The Linux system option uses sudo only to install and start the unit, then runs the application as the invoking user. The macOS installer creates a LaunchAgent. For a foreground process, `./biome serve` is sufficient. Stop that foreground instance before installing a service on its port. To update, run `git pull --ff-only`, run the tests, and restart the service. Standard standalone startup binds only to loopback. A direct network listener requires a TLS certificate and key:

```sh
./biome serve --listen 0.0.0.0 --port 8740 --cert /path/controller.crt --key /path/controller.key --advertise
```

## Use the application

The Work tab shows plan hierarchy, readiness and acceptance. Processes shows definitions, attempts, tokens, waits, results and provider traces. Capabilities lists offers and allocations. Knowledge exposes revision heads and conflicts. Attention collects human inputs, messages and enrollment candidates. The service console discovers the operations, shows their argument contracts, and invokes them.

```sh
./biome call bus.discover
./biome call work.create '{"id":"goal-one","kind":"goal","title":"Collect local observations"}' --id create-goal-one
./biome call cap.discover
./biome call process.list
./biome call knowledge.search '{"query":"observation"}'
./biome --realm local call work.list
```

Inputs may also be read from `@file.json`. Durable mutations have request IDs. Reuse an ID with the same arguments after a lost response. Reuse with different arguments is rejected. Scheduled work, human answers and callbacks are dispatched by the process runner. Its background loop advances only runnable BPMN-compatible instances and their subscriptions. The local realm has its own worker thread so a partitioned site backend does not stall local work.

* [Pi Docker CLI](providers/pi-docker/README.md) exposes a single argument-vector passthrough to Docker.
* [Pi terminal provider](providers/pi-terminal/README.md) installs the optional 16-session Bash terminal.
* [Processes, routines and providers](docs/runtime.md) covers BPMN, data bindings, enrollment, physical effects and adapter construction.
* [Multiple hosts and disconnected work](docs/deployment.md) covers quorum-backed control, offline grants, conflict resolution, boot services and recovery.
* [Qualification](docs/qualification.md) maps the specification’s scenarios to implementation checks.

## HTTP and MCP

The JSON bus accepts `POST /v1/call` with an operator bearer token or a signed scoped provider envelope. Example body:

```json
{"id":"list-1","realm":"site","operation":"work.list","arguments":{}}
```

The stateless MCP endpoint is `POST /mcp` with the same operator bearer credential. Its tools are `discover` and `invoke`. The former returns the live operation contracts and provider offers. The latter takes `operation`, `arguments`, an optional `realm`, and an application `request_id` for mutations. Clients that require a hosted OAuth authorization server need a suitable front end. The built-in operator session uses a local token. `GET /health` reports process availability and the most recent status of each realm.

## Test

```sh
python3 -m unittest discover -v
```

The tests use temporary node identities and databases. The optional three-voter failure lab is described in the deployment guide. Ordinary development and standalone deployment do not need Docker or etcd.
