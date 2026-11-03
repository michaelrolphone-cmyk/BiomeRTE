# Multiple hosts and recovery

## Pick the relationship between hosts

Cloning the repository copies the runtime. A normal `init` creates a distinct local node, public key and site identity. An independently initialized laptop or server has its own SQLite database until explicitly connected. The application supports two different deployment arrangements:

* **One site with replicated control.** Controllers use the same site ID and a dedicated etcd v3 namespace. Membership, elections and replication are provided by an established etcd cluster. Hosts independently generate their private operator and node keys. Install etcd on machines supported by that distribution; a 32-bit host can use the HTTPS gateway without running an etcd voter.
* **Independent/disconnected work.** Nodes retain local state and perform scoped work in their local realm. Trusted peers exchange signed revision pages and signed work packages. They can operate across intermittent links or carry packages on removable media.

### Connect controllers to an existing etcd cluster

On each controller, use a **new data directory** and the same site ID:

```sh
./biome --home "$HOME/.local/share/biomerte-shared" init --site-id field-team --etcd https://controller-a:2379 --etcd https://controller-b:2379 --etcd https://controller-c:2379 --ca /path/site-ca.pem --cert /path/node.crt --key /path/node.key
./biome --home "$HOME/.local/share/biomerte-shared" serve
```

Use trusted endpoints and authorize the controller to the `/biomerte/SITE_ID/` prefix. The optional `etcd.token` in the private node.json is an etcd authentication token if your deployment uses token authentication. The JSON gateway does not perform authorization from the TLS certificate common name. Configure transport trust, authentication, renewal and prefix access for the actual cluster. Only controller nodes need journal credentials. Preserve certificates/key files independently when backing up. Do not point an existing standalone database at a new cluster to merge histories. Migrate through signed application records and explicitly transfer hardware ownership.

Each site transaction reads the shared head, replays missing immutable journal entries into the local SQLite mirror, and commits its state changes, outbound commands and request receipt using a compare-and-swap on the head. The journal entry and head update are one etcd transaction. A concurrent writer gets a conflict; retry the same request ID. If an acknowledgement is lost, catch-up and the recorded receipt reconcile the result. Application controllers can accept requests at any reachable endpoint. The **quorum leader is in etcd**. Controllers that cannot contact a majority cannot commit site changes. The local realm continues to run. An authorized operator can request `--cached` on a local CLI read to inspect its last synchronized site view.

Use three voting members in separate failure domains to tolerate one loss. Operate membership changes, snapshotting, member substitution and disaster recovery with etcd administration tools. Application commands never promote an unfamiliar node to a voter. The application journal is presently append-only, and a new mirror replays its complete history. Size disk and monitor journal growth accordingly. One entry is limited to 900,000 bytes before transport encoding; large artifact payloads are chunked explicitly by callers. Use the browser’s **Controller status** button or the `cluster.status` bus operation to inspect member identities and observed leader/application positions. See the [etcd gateway API](https://etcd.io/docs/v3.5/dev-guide/api_grpc_gateway/) for the range and transaction protocol.

## Local work and rejoin

`./biome --realm local ...` selects the node-owned SQLite journal. Local work has its own routines, offers and mission profile. It does not fall back from a failed site command into local authority. Enroll local hardware in one enforcement domain; delegating a task does not make a disconnected copy its new exclusive hardware owner.

* **Knowledge, plans, messages and observations.** Writers identify revisions by digest and sign them with their node key. Each writer incarnation advances a local sequence and records parent revisions. `sync.export` pages causal dependencies first. `sync.import` verifies the package, the origin signature, recipient and writer sequence before advancing the cursor. Repeat or interrupted pages are safe. Trust each accepted origin with `sync`. For delivery into participant inboxes or event subscriptions, grant the origin `sync.mail` or `sync.events` respectively. Synchronization does not upload operator tokens, raw private keys, arbitrary control records, or task acceptance.
* **Concurrent edits.** `knowledge.get` returns all heads. `knowledge.merge` merges disjoint changes to sibling object revisions and reports conflicting field names. To resolve a field, provide its chosen head in `resolutions`. For longer histories or plain text, publish the merged result with all observed heads as parents. Work records generate plan revisions under `plan.TASK_ID`. After exchange, `plan.apply` materializes the chosen plan with current heads, validating hierarchy and dependencies. It preserves the local execution/acceptance evidence. To fork a plan offline, import its revisions, apply it, and edit it locally. Import ancestor plans first.
* **Exchange.** In each receiving realm use `peer.trust` on verified peer public keys. Run `./biome --realm local sync manifest.json` to pull signed pages from the remote URL and realm named in the manifest. The requesting key needs `sync.export` at the source; the receiver trusts the sender for `sync`. For two-way exchange, pull once in each direction. Reconnect and repeat to obtain only missing ranges. Air-gapped operators can transport the exact signed `sync.export` packet and pass it as the `packet` argument of `sync.import`.
* **Offline child work.** `offline.delegate` reserves a ready site task, pins all routine revisions and inputs, names the recipient, and lists allowed capability contracts. The local receiver trusts the issuer for `delegate` and `delegate:CONTRACT` for each contract. It runs `offline.accept` on the signed package in its local realm. It resolves those capabilities against locally commissioned resources. Unsupported or missing resources remain visible waits. Acceptance is idempotent. Return `offline.result` to `offline.reconcile` at the issuer, which verifies the worker key and `return` scope. A successful result moves the original task to review. Site-wide control transfers remain site transactions.
* **Mission capacity.** `mission.apply` takes a numeric budget, service priorities, dependencies and modes with nondecreasing costs. At each priority it funds minimums and then upgrades, accounting for a shared dependency once. Unfunded services are reported under `unmet`; new commands are held while in-flight commands can reconcile. Set `restore_margin` and `stable_samples` to hold upgrades until capacity recovers. Offers can name a service and compatible configuration modes. Resource measurements and physical low-power mode changes are supplied by providers.

## Backup and restoration

`./biome backup /private/new-backup-directory` makes consistent copies of both application databases, including artifact chunks, with private identity material. Protect the backup like a credential. Back up worker ledgers, physical adapter configurations and the etcd cluster at their owners. The copied site database on a shared controller is a mirror, not a replacement quorum. Restore into a different, empty directory:

```sh
./biome --home /path/new-node restore /path/backup
```

The restore retains identity, creates a fresh writer incarnation and sets a recovery hold. Retire or fence the previous owner, verify outstanding physical operations and restore the worker evidence before deliberately running `./biome --home /path/new-node resume-restore --previous-owner-retired`. A successful backup alone does not establish that a physical action can safely be repeated.

## Three-voter failure lab

On a Docker host with Compose, from this checkout:

```sh
docker compose -f deploy/etcd-lab.yml up -d
python3 -m tests.cluster_qualification
docker compose -f deploy/etcd-lab.yml down -v
```

This lab binds client ports 23791–23793 to loopback. The test finds and stops the actual leader, commits on the remaining majority, removes quorum, verifies independent local work, then restarts the members and checks convergence. The runners share one physical machine in the lab. For a deployed HA site, distribute the voters across hosts.
