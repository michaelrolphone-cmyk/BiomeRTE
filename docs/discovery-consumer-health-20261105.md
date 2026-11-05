# Discovery and consumer health — 2026-11-05

Mac terminal delivery is restored by PR #30. PR #31 adds discovery pages and consumer progress reporting.

Use biome_discover({}) to begin, then follow each returned next_call through biome_invoke. To select a section directly, use biome_invoke with operation system.discovery, arguments {section: "offers"} (also operations, contracts, guide), and a fresh request_id. Follow next_call until complete=true. Pages use live keyset ordering; registry changes between pages are explicit. The page-size target never drops an item or limits total inventory.

The hosted gateway currently intercepts the older discovery.page alias and filtered biome_discover calls and returns HTTP503. The verified system.discovery route reaches the physical controller through the normal authenticated invocation channel. The controller also retains discovery.page for local compatibility. Fixing the hosted shortcut still requires updating the hosted adapter; this is not represented as qualified.

biome_status now reports consumer_health separately from runtime and realm readiness. Outstanding uncertain effects and blocked assistant dispatches remain degraded; the presence of a queued or claimed operation is not success evidence. Readiness used for controller failover is unchanged.

The connector no longer rejects complete controller responses at 470,000 bytes. Default discovery uses pages, so inventory growth no longer causes the old large-response retry loop.

Validation:
- 115 native runtime tests passed on the Mac.
- Linux and macOS runtime, gateway, and replicated-recovery CI passed for the first PR31 commit.
- The 9 focused paging/gateway tests passed with the final compatibility alias.
- Default live discovery returned its operations page.
- Live system.discovery returned 7 offers from a registry containing 112 offers and a continuation call.
- biome_status exposes historical uncertain terminal effects while reporting the resumed assistant's running state.

The assistant recovery thread independently deployed the matching PR29 assistant and LM Studio fixes after terminal access returned. Its model selection and resource scheduling changes were preserved. The independent maintenance host enrolled and began heartbeats; its existing verification command remains awaiting explicit Android confirmation.
