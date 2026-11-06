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
boundary state with aggregate context. The two hidden-output branches also use
their accumulated signed write directions. The terminal RMS normalization uses
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

The code is an approximate numerical implementation, not an exact inverse or a
trained decoder. Software tests check state handling, numerical mechanics and
control flow; they do not establish semantic reconstruction quality. No full
9B-model inference, corpus trial or training run is part of this code update.
