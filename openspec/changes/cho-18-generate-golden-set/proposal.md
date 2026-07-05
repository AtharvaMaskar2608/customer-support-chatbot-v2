## Why

Retrieval-*quality* evaluation of the RAG pipeline needs a golden set: queries paired with the chunk that *should* be retrieved. CHO-16 (embedding cost/latency) and CHO-17 (retrieval latency) both measured speed; neither answered whether hybrid + RRF surfaces the *right* chunk. This change builds that golden set from the real `kb_faq` knowledge base using DeepEval's `Synthesizer` with Claude, unlocking the reference-based retriever metrics. Tracked as CHO-18.

The corpus has a trap: `kb_faq.chunk` is literally `"{topic} › {section}\nQ: {question}\nA: {answer}"`, so the `question` text is verbatim inside the chunk. Using the real question as an eval query makes retrieval trivially circular (exact full-text + near-identical vector match) → inflated, meaningless recall. So we generate **synthetic paraphrased** queries from each chunk instead, keeping the source chunk as the ground-truth retrieval target.

## What Changes

- Add a standalone `evals/quality/` area (sibling of `evals/embeddings/` and `evals/retrieval/`).
- Add `deepeval` and `anthropic` to the evals dependencies; wire Claude (`claude-sonnet-5`) into DeepEval via a `DeepEvalBaseLLM` wrapper (or the installed version's native Anthropic model class if present).
- Sample ~250 distinct chunks from `kb_faq` stratified by topic, pull via `asyncpg` (reusing CHO-17's connection pattern).
- Generate goldens with `generate_goldens_from_contexts` — one chunk = one context (1:1 ground truth), `max_goldens_per_context=1`, evolutions OFF, retaining a `context_index → kb_faq.id` map so each golden carries a `chunk_id`.
- Also generate a **~50-golden raw-question baseline set** from the real `kb_faq.question` values, saved separately — to later quantify the circularity gap (synthetic vs real recall).
- Save both datasets to `evals/quality/goldens/` with `input`, `expected_output`, `context`, and `chunk_id` per golden.

Non-goals (deferred): running the retrieval-quality metrics (the eval that *consumes* these goldens — CHO-19); generator metrics (AnswerRelevancy, Faithfulness) that need an agent generator; heavy evolutions / multi-hop goldens.

## Capabilities

### New Capabilities
- `golden-set-generation`: A repeatable harness that generates a paraphrased synthetic golden set (plus a raw-question baseline) from the `kb_faq` knowledge base using DeepEval's Synthesizer with Claude, retaining each golden's source chunk id as ground truth, and saves both datasets for downstream retrieval-quality evaluation.

### Modified Capabilities
<!-- None. New sibling capability; embedding-benchmark and hybrid-retrieval-benchmark are unaffected. -->

## Impact

- **New directory**: `evals/quality/` (`generate_goldens.py`, `claude_model.py`, `goldens/`).
- **Dependencies**: `deepeval`, `anthropic` added to `evals/requirements.txt`; reuses `asyncpg`, `python-dotenv` already present.
- **Config/secrets**: reads `OPENAI_API_KEY` (fallback if needed), `ANTHROPIC_API_KEY`, and `DATABASE_URL` (+ `PGPASSWORD`) from `.env` (already git-ignored). Read-only against `kb_faq`.
- **External calls**: ~300 Claude calls (generate + quality-score across ~250 synthetic + ~50 baseline goldens) — a few dollars, minutes.
- **No application code**: nothing in a future `src/` is touched.
- **Linear**: implements CHO-18; unblocks CHO-19 (retrieval-quality metrics).
