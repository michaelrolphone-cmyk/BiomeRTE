# Native retention rollout tools

These helpers require an explicitly reviewed host-specific plan and native qualification. They are not an automatic installer. Do not run a production phase without a current authorized execution window and strict admission/process proof.

`bundle_paths.rebase_spec` changes only exact caller-verified old private bundle roots. It leaves production targets and protected configuration paths unchanged. `verify_bundle_paths` checks the current root, all declared helper references, and every `--bundle` argument before dispatch. `assemble_bundle.py` performs the same check before creating the private staging directory.

Product qualification may be reused only when the complete fixture hash map and test modules/count are unchanged, the retained successful receipt and full log match, and interpreter/platform/SQLite match. Helper tests are always rerun natively after helper changes. A failed dispatch is never repeated blindly: first inspect the durable receipt, source preimages, service identity and offer restoration.

Platform helpers preserve existing provider offers and uncertainty records. They do not clear/cancel actions or declare unresolved historical effects quiescent. Mac process-boundary evidence establishes only safe idle-worker restart; actual active work still blocks. Pi uses independent service-cgroup evidence. All replacements are backed up and atomic with health/rollback receipts.

Run local tests from this directory with `python3 -m unittest native_helper_tests test_bundle_paths test_terminal_safety test_mac_safety test_mac_reconciliation test_mac_process_boundary test_mac_independent_services`. Platform-specific fixture variants must retain their own tests when creating a host package.
