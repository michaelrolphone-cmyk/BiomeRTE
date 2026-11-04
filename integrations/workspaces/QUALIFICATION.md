# Live qualification — 2026-11-04

Deployed Mac resource: `workspace.mac.75f0ef711b363088d0b1`. All 17 workspace contracts are advertised through BiomeRTE. Coding MCP exposes workspace_tools, workspace_start and workspace_status alongside the existing tools.

| Check | Evidence | Result |
|---|---|---|
| Container unit suite, including stale access, corrupt restore, active delegate and child protection | cap.status ws-final-deploy | 10 passed inside linux-basic image |
| Shared template reuse, three-agent handoff, stale generation rejection, root filesystem and volume persistence, authenticated GitLab, cancellation cleanup | cap.status ws-live-qualify-v2 | Passed |
| Real BPMN run binding, automatic human-wait suspension, next-step restore, normal completion cleanup | cap.status ws-bpmn-qualify; process b66d01f98d3943209f796aaebfbfa13b | Passed, five service calls |
| ESP-IDF image construction; tool versions; ESP32 hello_world compile | cap.status ws-esp-idf-v2-build | Passed |
| Cached ESP-IDF instance, SDK/QEMU commands, binary file round trip, failed-run cleanup | cap.status ws-esp-instance-qualify | Passed |
| Authenticated HTTP MCP discovery, workspace schemas, existing Git tools retained | cap.status ws-mcp-probe | Passed |
| BPMN XML example compilation | cap.status ws-final-deploy | Passed |

Ready shared images:

- linux-basic@1, linux/arm64: sha256:e1cb3f0da5f664e605020edf256495e99a0445dfa1cfaeb0ccc3fb4c80997df5
- esp-idf@2, linux/amd64: sha256:0335c310eae403ac2aa8df491c97de8bbb91e8437aac2f51a08b0dce19d6bada

The ESP template uses ESP-IDF 5.5.1 and Espressif Xtensa/RISC-V QEMU. Version checks and a real ESP32 compile passed; an emulated firmware boot and physical hardware flashing were not tested. The first ESP recipe failed because export.sh ran under sh; its immutable failed revision remains visible, and corrected revision 2 is ready.

Templates and checkpoints are stored in the Mac Docker store and verified host archives. Remote OCI replication, another workspace provider, cross-host restore and host-loss recovery drills are not implemented/qualified. Suspend restores filesystem state, not RAM or running processes. Agents publish required artifacts before ending a workflow. Private containers, volumes, checkpoints and checkpoint images are disposed; shared templates remain.

Qualification scripts retain operation IDs and receipts under the deployed workspace provider directory. Worker success is checked together with result.ok and command exit status. A workflow waiting on an active delegate does not imply that its workspace is idle.
