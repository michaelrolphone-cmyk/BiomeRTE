# Activation Field implementation status

Observed on the 24 GiB Apple Silicon Mac mini on 2026-11-04. This is an experimental build; useful memory has not been established.

## Verified native engineering evidence

Biome terminal command `af-full-model-qualification-2` returned exit 0 after 58.915 seconds. Its complete output is controller artifact SHA256 `5bd36005ff7705b43c4d0cf098a54b5b760a507c2a4b35f341c20f99613ad8a0`.

- 12 arithmetic/Metal/checkpoint/spectral tests passed.
- All 32 layers and 248 projections present.
- Disabled-field logits exactly matched the uninvolved forward path on the test tokens (maximum error 0).
- Zero baseline pull did not access the anchor during writes.
- Working packed codes changed; frozen tensors remained unchanged.
- Updated weights affected later logits (maximum difference 0.34375 on the test).
- Read-only probing preserved frequency state.
- Complete weight and hybrid cache checkpoint roundtrip passed.
- Exact change-map materialization reconstructed packed weights bit for bit.
- Field import reset all native caches.

Short-run measurements: disabled 0.110897 seconds/token; enabled 2.056513 seconds/token; MLX peak 12,051,067,470 bytes. These are two-token engineering measurements, not sustained chat throughput. Longer prefills, checkpointing, spectral work and queue wait add latency. Baseline model tensor allocation was 5,038,041,600 bytes, including 3,459,252,224 writable packed bytes. Peak estimated transaction allocation was 12,694,743,552 bytes.

Pinned model: `mlx-community/Qwen3.5-9B-4bit`, revision `8b2b98c00a6b4d291155e4890773ca8f769aee53`. Baseline identity: `5fe6cb50ccda48dc54ee1a3ae124fcd728f22966bd847572a261a87d629e63cc`. The Mac has a verified per-file manifest and Python dependency lock at the installation root.

## Pending verification

At this checkpoint, install command `af-install-v4` had been submitted through Biome, but the controller stopped replying before its result could be recovered. Service startup, capability registration, plugin compilation/install and an end-to-end agent request are not yet confirmed. Later local source changes add next-logit resume and repeated-seed qualification checks; those additional checks have not yet run.

Further recovery attempts after the reported controller fix still received no reply. Five additional model-free session-control tests pass locally: versioned config/idempotency, isolation/restart, config effective boundary, interrupted-request recovery, and synchronous reservation before starting a generation worker. The reservation and lock-order fixes need deployment to the Mac before its full test suite is rerun.

Full held-out memory, correction, 10,000-token stability, cross-domain retrieval and coherence experiments have not run. C3/C4 are explicitly marked unexecuted by the runner; exact matched replay is not implemented. Optional persistent homology and a calibrated coherence score are not implemented. The experiment runner is an initial harness, not a completed scientific study. Per-layer saturation/drift/update-norm reporting still needs expansion beyond current candidate/changed counts and receipts. Confirmatory multi-seed bootstrap analysis is not yet included.

The LM Studio SDK currently resolves to dependencies for which npm reports a constrained-collection validation advisory in Zod 3. No forced SDK downgrade or incompatible Zod major upgrade was applied. The authenticated loopback service itself does not use that dependency.

## Deployment recovery

Read `cap.status` for `af-install-v4` before retrying mutations. Fix any observed failed step, then continue from that step. Source package v4 is controller artifact `b5598b67b5107edb529aad18262ff2db456f512c1ccd709c0429e755b7e9805e`. The installed source directory and model download are independent of the controller checkout. Do not replace another agent's checkout or alter normal model/image admission.
