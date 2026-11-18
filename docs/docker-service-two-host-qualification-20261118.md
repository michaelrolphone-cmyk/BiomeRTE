# Actual Pi and Mac Docker failover qualification

**Passed on 18 November 2026:** a harmless stateless HTTP service transferred between the actual Raspberry Pi and Mac through the deployed HTTPS coordinator. The Pi's production Docker supervisor is installed and running. The test used dedicated qualification workers; the production Mac supervisor remained unchanged.

The complete machine-readable result is [docker-service-two-host-qualification-20261118.json](docker-service-two-host-qualification-20261118.json). The executable procedure is [qualify_docker_services_two_hosts.py](../tests/qualify_docker_services_two_hosts.py).

## Measured result

| Check | Observed result |
| --- | --- |
| Initial service on the actual Pi | Healthy and active after 22.507 seconds of observation |
| Automatic distribution | An unseeded Mac worker learned the exact manifest from the coordinator |
| Production isolation | Both real production workers reported `dependencies_not_ready` for the qualification service |
| Pi test supervisor becomes unresponsive | Mac became healthy and active 15.129 seconds after pause acknowledgment |
| Old ownership | The coordinator rejected the former Pi grant after Mac takeover |
| Pi recovers | It removed its stale child and remained standby while the Mac stayed healthy |
| Mac test child crashes | Pi became healthy and active 27.798 seconds after kill acknowledgment |
| Cleanup | Dedicated test containers were removed on both hosts; production supervisor identities, images, running states, and restart policies were unchanged during the test |

Timing starts after the fault-injection command returns. These are measurements from this run, not a guaranteed availability bound.

This test used `effect_policy=stateless` and a disposable HTTP process. It did not exercise the physical relay, power off either host, migrate application state, or prove physical effect fencing. A paused supervisor can temporarily leave its stateless child running; rejection of its old coordinator grant is separate from enforcement at a physical device. Stateful and effectful services need their own adapter, checkpoint, and effect-admission qualification.

## Actual deployment and images

Both images contain the exact runner and adapter source from local commit `b738a0bf36cf741c623b30cc832be9e662a177d8`. That source passed 81 unit tests, a Python 3.4 grammar check, and the subsequent actual-host qualification.

| Host | Docker engine | Platform | Immutable supervisor image |
| --- | --- | --- | --- |
| Raspberry Pi, ProximityRF | 17.06.2-ce; server API 1.30 | linux/arm/v7 | `sha256:e5ddd5f2cc178b8bb4ce23f451bb6fab8f16c2c47529a84f0ac2676bb16f904e` |
| Mac Docker Desktop | 24.0.7 | linux/arm64 | `sha256:5d7f7df07cf4166175a35c74c23fa15f3ca984db0f56fdc86b622662dbdc908c` |

The runner continues to use Docker API 1.24. Images were built using the Mac's buildx with an explicit platform, then the ARMv7 image was streamed through pinned SSH into the Pi's Docker loader. No Pi OS, engine, kernel, native controller, or native capability service was replaced.

The actual Pi image imported both modules and contacted its Docker API successfully. Its production supervisor is `biomerte-service-runner`, container `301fb26071e49429aa432c4a09ec2c28197c2564c086cf1c5438cc077e357b7a`, with restart policy `unless-stopped`.

Persistent Pi paths:

- Configuration: `/home/pi/.local/share/biomerte-docker-services/config/runner.json`
- Existing private coordinator key: `/home/pi/.local/share/biomerte-docker-services/config/coordinator.key`
- Current local state: `/home/pi/.local/share/biomerte-docker-services/state`

The Pi configuration uses `docker_request_timeout_s=60`. Its pre-upgrade configuration was preserved as `runner.json.before-b738a0bf-20261118`. The replacement was guarded by the expected supervisor label and a check that the production worker owned no managed service children.

The Mac image above was used by its dedicated qualification supervisor. This task did not replace the Mac production supervisor. The report records both production supervisors before and after qualification.

## Why the timeout and supervision changes were needed

The original actual-Pi attempt logged a service start transport timeout under the original 4-second Docker budget. A separate disposable probe measured:

| Operation | Elapsed seconds |
| --- | ---: |
| Create | 0.290 |
| Start | 1.603 |
| First observed healthy state | 11.815 |
| Managed removal | 31.101 |

Two health subprocesses exceeded a 2-second budget before the next succeeded. The probe omitted published ports, so these values do not identify which substep timed out in the original qualification attempt.

The runner now performs startup and removal outside the grant lock, uses a separately configurable Docker operation budget, checks the exact current binding before create/start and after a late response, and retains unresolved named-container custody. A lost response does not justify a retry under a new name. Persistence-failure and multiple-orphan cleanup cases are covered by the unit tests.

For this actual-host test, the Docker request budget was 60 seconds and the health-command budget was 5 seconds. Heartbeats remained 1 second and the manifest lease was 12 seconds. The request budget does not limit a service's execution lifetime. Lease expiry and revocation remain separate from slow Docker cleanup.

## Isolation and cleanup

The public witness was the existing deployment at:

https://biomerte-stream-gateway-e0322b2b60c9.herokuapp.com

It was not restarted during qualification.

The test created workers `qa-pi-pi-mac-20261118-03` and `qa-mac-pi-mac-20261118-03` and service `qa-two-host-pi-mac-20261118-03`. Its readiness check addressed port 49177 on each supervisor container's own loopback interface. Only the dedicated wrappers served the run-specific readiness path. The harness verified that both ordinary production supervisors were ineligible before injecting faults.

The harness paused only the dedicated Pi supervisor and killed only the dedicated Mac test child. Cleanup checked the exact run labels and service/worker labels. Duplicated qualification keys were removed; diagnostic configuration, local state, and reports were retained.

The coordinator has no catalog-deletion operation. The service was therefore retired with a guarded revision compare-and-swap to an unserved readiness path, and the harness waited for ownership to drain. The retained disabled revision is:

`a1f8481042f70ef360bb6f283884fe522c540c432ef58949b86c7a9051eb3eca`

The earlier first attempt also left a disabled declaration. The second attempt never registered a declaration because its wrapper failed before runner startup.

## Repeating the actual-host procedure

Use a new run ID and an unused output filename. Both immutable images must already be present on their intended hosts. The existing Mac configuration supplies the authenticated coordinator connection; the script does not print the key.

Run on the Mac through its authorized terminal capability:

```sh
python3 -u -B -S tests/qualify_docker_services_two_hosts.py \
  --pi-host 192.168.1.23 \
  --pi-image sha256:e5ddd5f2cc178b8bb4ce23f451bb6fab8f16c2c47529a84f0ac2676bb16f904e \
  --mac-image sha256:5d7f7df07cf4166175a35c74c23fa15f3ca984db0f56fdc86b622662dbdc908c \
  --run-id NEW-UNIQUE-RUN-ID \
  --output docs/NEW-QUALIFICATION-REPORT.json
```

SSH uses the saved `biome-sacred-pi` host-key pin, the existing Mac admin key, strict host-key checking, batch mode, and the scoped legacy RSA signature option required by this Pi. If the address changes, resolve it using the saved host-key identity before rerunning.

A failed or interrupted operation must be reconciled through its durable terminal receipt and run report before retrying. The harness rejects reuse of an existing qualification directory. Its 180-second observation windows apply only to this disposable test.

The wrapper adds `/app` to Python's import path before calling `runpy`, so the runner can import its sibling adapter module. A supervisor exit is checked during progress observations and is reported as a test-process failure.

## Preserved evidence

- [Original Pi baseline and latency measurements](docker-service-pi-baseline-20261118.json)
- [Pi compatibility deployment](docker-service-pi-deployment-20261118.json)
- [Attempt 1: original Docker transport timeout](docker-service-two-host-attempt1-20261118.json)
- [Attempt 2: qualification wrapper import-path failure](docker-service-two-host-attempt2-20261118.json)
- [Attempt 3: raw successful run](docker-service-two-host-attempt3-20261118.json)
- [Successful result with source and receipt identities](docker-service-two-host-qualification-20261118.json)

The successful terminal operation was `pi-mac-docker-failover-live-20261118-20`, exit 0, stdout artifact SHA-256 `b70a6530d533a0e3b1cb82c18c654e11b56137e6df64895986e173a444453764`. The reports also preserve the failed attempts and their cleanup rather than treating them as successful qualification.
