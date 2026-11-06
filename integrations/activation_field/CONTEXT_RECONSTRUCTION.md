# Approximate reverse propagation

The path now accumulates signed propagation information while the field's
quantized weights evolve, propagates combined present state backwards through
operation counterparts, and emits a token sequence. It stops there.

## Forward state

The 248 writable projections retain fixed-size signed input/output moments,
forward movement and bounded write-coefficient moments. Each decoder layer and
the final normalization retain running boundary means, current normalized output
and a small signed movement matrix. Boundary state is aggregated online even
when weight commits are batched; no per-token boundary history is kept.

Linear-attention layers retain their current recurrent matrix, which is already
an aggregate state, and the mean of the convolution context. Full-attention
layers reduce the currently attended keys and values to one mean cell per head.
They do not retain a sequence of KV entries for reverse propagation. These
summaries are approximate context for the reverse operations.

Direction and boundary tensors use the existing authenticated encrypted
checkpoint format. Snapshots include the adjusted packed weights and those
aggregates. They exclude original token IDs, source text, logits and ordinary
cache shards. Legacy checkpoints still load for ordinary use, but cannot supply
direction information that was never collected. Reverse jobs on them fail
explicitly instead of reverting to token search.

## Reverse operations

The runtime loads the current adjusted weights into a private engine. A numerical
vector-Jacobian product through each actual Qwen decoder layer supplies its
approximate counterpart, including its residual, normalization, nonlinear gate,
attention, convolution and recurrent operations. It is evaluated at accumulated
boundary state with aggregate context. The two hidden-output branches also add
their accumulated signed write means at their recorded coefficient magnitudes.
Each mean is no longer independently amplified to unit length. Only the
combined incoming signal is normalized. This conditioning remains approximate,
not a derived inverse of the weight update. The terminal RMS normalization uses
a directional scale reversal so a radial signal is not erased by its derivative.

The reverse trajectory begins from the normalized combined current/mean terminal
state. The accumulated movement matrix supplies its backward movement. Recorded
motion determines how far it advances; a vanished movement direction ends it.
There is no configured token count, replay search, source-activation match,
restored-state requirement or quality threshold.

## Direct token readout

A reusable coordinate-partition tree is built once from the frozen embedding
table and bound to the model baseline. Building this index reads and partitions
the vocabulary embeddings. Each emission then follows coordinate comparisons to
one token ID. It does not scan vocabulary rows, simulate token candidates, rank
candidate sequences or replay tokens through the forward model.

The tree is an approximate vector quantizer, not a trained language decoder.
The emitted sequence follows the reverse trajectory. Its linguistic quality,
subject retention and resemblance to any original wording are unmeasured.

## Interface and limits

Existing Biome start/status/cancel/export routes remain. Start accepts a target
checkpoint and `options: {"mode": "reverse"}`; this is also the default. The former
`first_pass`, `replay`, candidate-pool, beam and fixed-token-count options are
rejected. Their offline numerical helpers remain only for historical artifacts.
No generated token is reinjected, reinforced, summarized or polished by this path.
Protected export packages the raw emitted text and IDs without changing them.

## Observational reverse diagnostics

Set `options: {"mode": "reverse", "debug": true}` to include a compact diagnostic
trace in the existing encrypted result and its explicit protected export.
Debugging defaults to off. It does not change reverse arithmetic, write-direction
conditioning, normalization, stopping rules, emitted IDs or token count. It
does not run an extra model evaluation, scan the vocabulary or retain input
tokens, source text, full reverse vectors, or a forward activation history.

The trace records each emission's signal norm, change from the preceding
emission, cosine, and normalized chord change. Layer measurements bracket the
existing write-direction addition and the VJP. Separate measurements report
the raw write-mean norms, combined write magnitude, and how much
conditioning changes the incoming direction. Trajectory measurements report
the raw movement, its radial and tangential components, actual normalized-state
movement, and which existing motion, small-step or zero-direction stop fired. These are
observations, not new correctness thresholds or stopping criteria.

Readout diagnostics describe the exact comparison vector and visited partition
branches, along with the closest split and its margin. Identical paths are
stored once. This distinguishes an unchanged upstream signal, variation lost
during propagation, and changing readout vectors that remain in one partition
region. Equal token IDs alone do not distinguish those cases.

Debug schema version 2 names the bias magnitude `combined_write_norm`; the Qwen
result marks conditioning as `recorded_signed_magnitude`. The earlier instrumentation-only
schema 1 used `combined_unit_write_norm` for the unit-amplified implementation.

Only one preceding vector per observed signal is kept transiently. Serialized
trace storage is scalar-only and proportional to emitted steps and model
layers; signal names and column names are stored once. It is never written to
ordinary job progress or plaintext logs. The result's existing source reference
binds the trace to its checkpoint. Completed runs retain the full trace; a run
that fails before producing a result does not yet export partial diagnostics.

The code is an approximate numerical implementation, not an exact inverse or a
trained decoder. Software tests check state handling, numerical mechanics and
control flow; they do not establish semantic reconstruction quality.

Native saved-checkpoint validation with the full 9B model reproduced 489 copies
of one token before the magnitude correction. Preserving recorded write
magnitudes produced 489 outputs spanning 34 token IDs, with no consecutive
identical IDs. The terminal trajectory and motion-exhausted stopping point were
unchanged. Consecutive normalized readout movement increased from roughly
1.8e-7 to a median of 0.888. The resulting raw text is still incoherent: this
validates removal of the measured signal collapse, not semantic reconstruction.
The native suite passed 172 tests with no skips. No new ingestion, corpus trial
or training was performed for this validation.
