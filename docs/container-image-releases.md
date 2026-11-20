# Container image releases before and after a registry

The existing service coordinator and Docker supervisor remain the deployment mechanism. This utility supplies immutable image references. It does not provision a registry, start a service, copy a controller database, or declare a standby ready.

## Build and preserve a release now

Use Python 3.9+ on the build host and a committed source revision. The utility exports that Git tree into an isolated build context, excluding ignored credentials, runtime state, model files and unrelated workspace files. OCI labels identify source and revision. Public base images are resolved to the requested platform's registry manifest digest before building, avoiding ambiguous multi-architecture tags in classic Docker stores.

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

## Private GHCR publication from qualified main

The Publish private container images workflow runs after BiomeRTE qualification
succeeds on main. Manual dispatch is accepted only on main and still requires
the latest qualification run for that exact commit to have succeeded. The
workflow checks out the qualified commit explicitly and rejects fork/PR runs.
It builds and smoke-tests service-runner, service-authority and portable-services
for linux/arm64 and linux/arm/v7 through the common tool above.

The build and publish steps share a temporary GitHub-hosted runner. This
repository is public, so image archives are deliberately not uploaded to
Actions artifacts. Only nonsecret source/qualification/visibility/digest
receipts are retained as the ghcr-release-RUN-ATTEMPT artifact for 90 days.
Copy these receipts into the deployment's retained release records. A partial
push preserves every confirmed digest in images.lock.json; publication alone
does not activate or replace any workload.

The workflow uses the built-in GITHUB_TOKEN with packages:write. It supplies
that token only to the publication checks/login step, does not persist checkout
credentials, and logs out afterward. No account PAT or repository secret is
needed to publish to existing private packages that grant this workflow access.
Existing destinations must be verified private. An absent package is allowed
only after the actual publishing repository is verified private through the
GitHub API. The workflow repository comes from GITHUB_REPOSITORY; an explicit
--repository cannot override that identity. Missing publishing context and
public, internal, or unknown repository visibility fail before any push.

GitHub Actions creation using GITHUB_TOKEN inherits the publishing repository's
visibility and permissions. It must not rely on the generic Container registry
default-private statement. First creation therefore belongs in a verified
private publishing repository, or the package must already have verified private
visibility and grant this workflow access. A second check verifies all packages
are private after publication. An unreadable or nonprivate destination fails
the run rather than changing visibility; errors report the actual visibility.
Private publishing-repository metadata is a first-creation precondition, not
proof of the created package's visibility. The combination of a private publishing
workflow and an OCI source label linking a different public repository still
needs operational verification. Retain postflight and verify anonymous access
is denied before treating that release as private.
See GitHub's Actions-specific defaults:
https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions#default-permissions-and-access-settings-for-packages-modified-through-workflows

The repositories are:

    ghcr.io/michaelrolphone-cmyk/service-runner
    ghcr.io/michaelrolphone-cmyk/service-authority
    ghcr.io/michaelrolphone-cmyk/portable-services

Tags retain the source commit prefix and platform; deployment references use
the full confirmed registry digest. The same private-visibility guard is
available to dependent image workflows:

    python3 deploy/docker-services/ghcr.py private --owner OWNER --image IMAGE \
      --output private-before.json
    python3 deploy/docker-services/ghcr.py private --owner OWNER --image IMAGE \
      --require-existing --output private-after.json

GH_TOKEN authorizes these read-only GitHub checks. It never enters a receipt.
For platform builds qualified separately, combine exact-source releases with:

    python3 deploy/docker-services/images.py combine \
      --lock /path/to/arm64/images.lock.json \
      --lock /path/to/amd64/images.lock.json \
      --output-dir /path/to/combined

Combine validates every input archive and matching source/top-level provenance,
rejects duplicate targets, preserves per-image metadata, and rechecks copied
archives before completing the combined lock. It neither rebuilds nor publishes.

Private deployment pulls need separate read:packages credentials. The Actions
publication token is job-scoped and must not be copied to a persistent host.
A registry release does not establish physical Pi compatibility or state/effect
handoff for Rover and ToF.

## Separate registry names from logical image IDs

An optional --repository-prefix changes only the registry repository name at
publication. For example, --repository-prefix biomerte- publishes logical image
service-runner as ghcr.io/OWNER/biomerte-service-runner. The default empty prefix
preserves existing names. Prefixes cannot contain slashes, credentials, ports or
uppercase characters.

The original logical ID, exact source/commit/platform labels and image ID remain
unchanged and are all checked before any tag or push. The confirmed registry_ref
records the actual destination; render continues selecting the logical image ID.
For a prefixed destination, the GHCR private guard's --image must name the actual
registry package (for example biomerte-service-runner), while build/render keep
using the logical ID service-runner. No existing workflow changes its package
names merely because this optional publication argument is available.
