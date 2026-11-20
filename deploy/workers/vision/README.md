# Vision worker packaging

## Current Mac profile

The small `Dockerfile` packages the actual capability implementation, all 47 declared
vision/video/map/rig contracts, leased front/rear/map streaming, and the deployed
vision source modules. Its running provider delegates native compute and map state
to the existing authenticated HostIO boundary. The Mac's detector remains on its
configured Metal/MPS backend; the native SLAM, maps, dual camera, sensor fusion and
video-job implementation remain authoritative. The small provider image is not a
claim that Metal compute runs in a Linux VM.

Source is imported from the deployed `ftm-only-20261113` release. Despite that
directory name, this release includes the full mapper/detector/camera/sensor
implementation. `source-import.json` records original source hashes before these
portability changes, including the matching motion source imports.

## Build and qualification

From the repository root:

```sh
docker build -f deploy/workers/vision/Dockerfile -t biomerte-worker-vision-provider:<source-sha> .
docker run --rm --network none --read-only --tmpfs /tmp biomerte-worker-vision-provider:<source-sha> qualify
```

The isolated qualification exercises actual provider dispatch and imports leased
streaming with a fixture native boundary. It does not acquire a live worker identity,
change the detector device, advertise offers, or invoke physical effects.

## Runtime configuration

`render-config.py` copies the existing native provider manifest, preserves
provider/site/resource/capacity/state identity and adjusts only packaged executable
and transport paths. It disables the native-only loopback stream URL optimization;
gateway-issued stream endpoints remain in use.

```sh
python deploy/workers/vision/render-config.py --native-manifest /path/to/provider-manifest.json --output /path/to/container-provider.json
python deploy/workers/vision/native-policy.py --native-python /path/to/vision/env/bin/python --output /path/to/vision-policy.json
```

The second command renders the value for existing HostIO `vision_queries`:
an exact `operations` allowlist, `mutations` list, and explicit native
`python`. It does not update an active bridge configuration. Native
`device_routes_module_directory` points to the deployed vision source;
`vision_directory` points to the existing native state. Mutating queries also
require existing top-level `enable_effects=true`. Read-only qualification can
enable status, snapshot and video import read while keeping effects disabled.

Set variables in `compose.yaml`. `VISION_PROVIDER_STATE` is the native
vision state's `provider` subdirectory, mounted at the same absolute path.
Do not mount native map databases, Unix sockets, model files, or rotating OAuth
credentials into this provider. All queries, front/rear/map frames, video-job
state and native credential rotation use the existing boundary. Native video
imports run with the configured original vision interpreter, including child
video workers.

Stop the native provider before starting this container with its existing provider
identity and durable worker/stream sequence state. Keep the native vision backend
running. Cross-kernel file locks cannot prevent duplicate native/container owners.
The deployment owner controls this handoff and rollback; build/render/qualification
commands make no live service changes.
