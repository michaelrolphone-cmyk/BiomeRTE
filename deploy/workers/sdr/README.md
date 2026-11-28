# Pi SDR worker image

`worker-sdr` packages the existing receive-only SDR provider and daemon for
Linux ARMv7. The default `check` command imports the deployed code and locates
the receiver libraries without opening a device.

For a supervised handoff, stop the native SDR provider and daemon before
reusing their existing identity. Mount the SDR provider home and receiver
state at explicitly chosen container paths and run
`serve --home /run/provider --state /run/receiver`. The mounts must be
writable by UID 1000. Map only the USB or serial receiver devices and groups
needed by the existing configuration. The configured controller URL must
remain reachable from the container.

The launcher waits for the daemon to listen on its Unix socket before
advertising the capability. It leaves the enrolled manifest and existing
receiver state in place. Hardware and live controller qualification remain
specific to the Pi and attached receiver.
