# Gateway source provenance

The gateway directory is synchronized with the authoritative hosted gateway
source from commit 00da76b404147902af5c8b23b2f6c490921b4686. That source
descends from the deployed version 83 baseline
e268e9b8dc170f6ac3a8beec2d16cf2d35ae6f32.

The older gateway directory omitted live maintenance, local controller routing,
provider placement, dispatch ownership, journal recovery, and dashboard code.
This synchronization preserves those deployed behaviors while adding controller
network observations to the existing registration.

The hosted source's tracked files are mirrored under gateway/. The local
gateway/.openai/hosting.json remains ignored and deployment-specific.
gateway/hosting.example.json preserves the required DB and JOURNAL_ARCHIVE
bindings with a placeholder project identifier. Production publication must
retain the selected Site's existing identity, audience, and environment.

Build the gateway before running its tests: bundle tests exercise the produced
Worker. CI follows the same order and supplies Python for the existing signed
native-call fixtures.

The root deploy/ and biomerte/ directories remain authoritative for installed
controller and gateway-link releases. gateway/deploy/ contains the hosted
source's historical client fixtures; synchronizing it does not replace the
current root controller deployment.

For network reporting and route-reference semantics, read
controller-address-registration.md and
../gateway/docs/controller-network-registration.md.
