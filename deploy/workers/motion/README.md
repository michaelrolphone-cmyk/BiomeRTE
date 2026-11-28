# Rover motion capability worker image

`worker-rover-motion` packages the current training, prediction and planning
capabilities for Linux ARM64. Its default `check` command runs the motion
model, capability, planning and training tests.

To reuse an enrolled worker, mount its existing worker state, model database
directory and dedicated machine gateway identity. Mount the model database
directory at the same absolute path stored in the provider configuration
because the native observer writes there. Put the gateway configuration and
the worker's scoped key at `/run/biome/mac-terminal`. Stop the native
capability worker before running `serve --state /run/motion-worker`.
The persistent observer remains the producer of the model database.

The packaged worker resolves code paths inside the image while preserving
the configured provider, resource and database. Exercise the actual
observer, controller admission and model updates before a live handoff.
