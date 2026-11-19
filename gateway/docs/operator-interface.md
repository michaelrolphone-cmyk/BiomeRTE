# BiomeRTE operations console

The authenticated dashboard uses the enterprise console layout with live controller data. Workflow definitions and runs have full-size diagrams, zoom, pan, fit, full-screen, step selection, and run token highlighting. The graph for a run is read from that run's exact immutable revision. Missing retained history is explicitly reported.

Capability navigation is derived from native identities as a namespace tree. Names lead; exact identities are secondary. Parent namespaces can contain both executable capabilities and child namespaces.

## Mapping metadata

The tracked dashboard/mappings.json contains defaults as records with kind, identity, metadata.name, metadata.summary and, for workflows, metadata.steps. Step metadata keys combine the exact revision and node ID. Renaming changes metadata, never the execution identity. Owner edits persist in D1 dashboard_mappings, separated by realm and merged with defaults. The Settings page and each detail view provide name editing.

## Preserved actions

Work creation and acceptance, workflow start and cancellation, decision responses, knowledge read/write/conflict inspection, enrollment review, demo execution and direct service operations remain available. Raw identities, provider contracts and records are shown in Advanced details and Advanced tools.

## Backend

Owner-only /dashboard/mappings stores display metadata. Owner-only /dashboard/definition relays exact graph and compact run-summary reads to deploy/dashboard_read.py through the established controller connection. That module opens the selected controller store read-only. It never updates workflow definitions or run state. Install the matching dashboard host adapter on each serving controller.

The overview counts recorded runs and live decision waits. Capacity is counted once per resource rather than once per contract. Times and historical step completion are not fabricated when source data does not retain them.
