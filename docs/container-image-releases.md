# Container image releases before and after a registry

The existing service coordinator and Docker supervisor remain the deployment mechanism. This utility supplies immutable image references. It does not provision a registry, start a service, copy a controller database, or declare a standby ready.

## Build and preserve a release now

Use Python 3.9+ on the build host and a committed source revision. The utility exports that Git tree into an isolated build context, excluding ignored credentials, runtime state, model files and unrelated workspace files. OCI labels identify source and revision.

Build architectures sequentially with the existing Docker daemon. A classic image store loads one architecture per build. The initial catalog contains the supervisor and service authority; add qualified application Dockerfiles as they are migrated.

    python3 deploy/docker-services/images.py build \
      --image service-runner --image service-authority \
      --platform linux/arm64 --platform linux/arm/v7 \
      --output-dir /absolute/path/to/image-releases/REVISION

The repeated platform arguments build both architectures sequentially into one release lock. A build does not qualify an unavailable Pi's kernel, hardware, state, network or authority behavior.

Each output directory contains Docker-loadable archives and images.lock.json. The lock records exact source commit/URL, base image digest, platform, immutable local image ID and archive size/SHA-256. It remains incomplete until all requested builds and smoke checks succeed.

    python3 deploy/docker-services/images.py verify \
      --lock /absolute/path/to/image-releases/REVISION/images.lock.json

Transfer archives and lock to an eligible host, verify there, then use docker load --input IMAGE.tar and check the expected local image ID. The existing runner prepares the image before advertising eligibility.

Use render --image NAME --template SERVICE.json --lock LOCK --local --output MANIFEST.json to fill an existing manifest's image map and recompute its revision. The rendered image map contains exactly the platforms in the selected release lock; a one-platform lock produces a one-platform manifest. Use a combined lock to retain both Mac and Pi architecture entries. Local IDs require preloading on every eligible host. Rendering does not submit or activate the manifest.

## Connect a registry later

Create the repository/namespace at the chosen registry when available. Use Docker's credential helper or docker login --password-stdin outside the build. Credentials never belong in images, build arguments, manifests, commits or release locks.

    python3 deploy/docker-services/images.py publish \
      --lock /absolute/path/to/image-releases/REVISION/images.lock.json \
      --registry ACTUAL_REGISTRY_HOST --namespace ACTUAL_NAMESPACE

Publishing uses local immutable images and records each confirmed push digest as repository@sha256:digest. Separate architecture variants match the existing supervisor's platform selection. No mutable latest tag or new distribution service is required.

Render without --local after publication to select registry digests. Deploy using the existing revision-checked manage.py declare command and applicable state/effect adapters. Registry availability alone does not establish state portability, freshness, health or cross-host failover.

## Operational boundaries

Keep the controller, independent maintenance route, updater, host terminal, Docker host adapter and native accelerator adapters available during application replacement. Images contain code; private configuration and authoritative state use existing private bindings and qualified state adapters.

Docker's Linux VM has less free disk than the native host. Use shared lightweight layers, sequential builds and guest-space observations. This utility never prunes data, changes the storage mode or restarts the daemon.
