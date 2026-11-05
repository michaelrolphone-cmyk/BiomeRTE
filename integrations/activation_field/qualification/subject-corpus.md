# Subject-realm corpus experiment

The acceptance criterion is recognizable subject matter and related concepts in reconstructed context. Different wording and order are expected. Exact token matching is neither required nor deliberately prevented.

Corpus 01 contains 415 self-authored words about a fictional community orchard's irrigation, soil care and seasonal maintenance. The source is ingested in a new persistent Activation Field session. Only user-role input changes weights; assistant and template writes are disabled. The one generated assistant token is excluded from weight updates.

The reconstructor receives the frozen checkpoint ID, a neutral purpose ("corpus trial 01"), and explicit search options. It receives no reference text, topic label, evaluation keywords, source tokens, caches or logits. Source text and review criteria exist only in the separate experiment harness.

The initial search uses 24 output-token positions, 16 vocabulary proposals, beam width 2, sketch rank 8 and four power iterations. These are visible trial settings, not system limits. Replay fit is a numerical diagnostic; it does not establish topic relevance or historical accuracy.

Run the continuation after the already-submitted `afctx-corpus01-ingest` operation:

```text
python qualification/run_subject_corpus_01.py qualification/subject-corpus-01.json
```

It resumes stable capability operation IDs, checkpoints the isolated source, waits for the shared model queue, exports the raw context, and verifies integrity, source preservation and full-model replay. It records `~/.local/share/biome-activation-field/subject-corpus-01.json`. A process lock prevents duplicate local supervisors. It has no runtime cutoff and does not interrupt the transform fit.

The final status is `awaiting_subject_review`, not an automatic pass. Review the raw reconstructed text for:
- recognizable orchard / soil / water subject matter;
- related concepts, including paraphrases;
- coherence and unsupported drift toward unrelated subjects.

Literal overlap and replay residual may be reported but are not semantic acceptance thresholds. Preserve raw output; do not rewrite it into the intended subject. The existing exact synthetic mechanics regressions remain useful implementation checks and are separate from this corpus evaluation.

Current state: ingestion has been submitted but is queued without provider admission. No corpus result has been measured.
