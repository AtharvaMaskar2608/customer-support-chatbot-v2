## Why

CHO-16 (embedding cost/latency) and CHO-17 (retrieval latency) measured whether the RAG pipeline is *fast*; neither answered whether hybrid + RRF surfaces the *right* chunk. CHO-18 built the golden sets that make that measurable. This change (CHO-19) consumes those goldens to finally answer the quality question: **does the CHO-17 hybrid RRF pipeline retrieve the ground-truth chunk, and how well is it ranked?** It also delivers the payoff CHO-18 was designed for — quantifying the circularity gap between honest synthetic-query recall and inflated raw-question recall.

## What Changes

- Add a retrieval-quality eval runner under `evals/quality/` (sibling of `generate_goldens.py`) that loads the CHO-18 golden datasets and evaluates the CHO-17 RRF retriever against them.
- For each golden: embed the `input` (`text-embedding-3-large` @1536, matching `kb_faq.embedding`), run the RRF SQL (reused from `evals/retrieval`) to get top-K chunks, and treat those as `retrieval_context`.
- Compute two metric families:
  - **Ground-truth `chunk_id` metrics (cheap, deterministic, no LLM):** recall / hit@k (k=1,3,5,10) and MRR against the known `chunk_id` — the primary, objective signal.
  - **DeepEval reference-based metrics (LLM-judged, Claude `claude-sonnet-5`):** ContextualRecall, ContextualPrecision, ContextualRelevancy — reusing the native `AnthropicModel` wiring in `evals/quality/claude_model.py`.
- **Key deliverable:** run both the synthetic set and the raw-question baseline and report their recall **side by side** to quantify the retrieval-circularity gap.
- Report **per-topic** recall/hit@k so weak knowledge areas are visible, and optionally filter synthetic goldens by `synthetic_input_quality ≥ 0.5`.
- Save results as timestamped JSON + CSV under `evals/quality/results/` (gitignored).

Non-goals (deferred): generator/answer-quality metrics (AnswerRelevancy, Faithfulness) that need an agent generator; cross-encoder re-rankers; any changes to the retriever itself or to `kb_faq`.

## Capabilities

### New Capabilities
- `retrieval-quality-eval`: A repeatable harness that evaluates the hybrid RRF retriever against the CHO-18 golden sets, computing deterministic ground-truth `chunk_id` metrics (recall, hit@k, MRR) and DeepEval reference-based retriever metrics (ContextualRecall/Precision/Relevancy) with Claude, and reporting synthetic-vs-baseline recall and per-topic coverage.

### Modified Capabilities
<!-- None. New sibling capability; golden-set-generation, embedding-benchmark, and hybrid-retrieval-benchmark are unaffected (read-only consumers). -->

## Impact

- **New file(s)**: `evals/quality/eval_retrieval.py` (runner); `evals/quality/results/` (gitignored, with `.gitkeep`).
- **Reuses**: CHO-18 goldens in `evals/quality/goldens/`; the RRF SQL + embedding recipe from `evals/retrieval`; the Claude model wiring in `evals/quality/claude_model.py`; `asyncpg`, `openai`, `deepeval`, `anthropic` already in `evals/requirements.txt`.
- **Config/secrets**: reads `OPENAI_API_KEY` (query embedding), `ANTHROPIC_API_KEY` (metric scoring), and `DATABASE_URL` (+ `PGPASSWORD`) from `.env` (git-ignored). Read-only against `kb_faq`.
- **External calls**: ~300 query embeddings (cheap) + up to ~900 Claude metric-scoring calls across the 3 LLM metrics × ~300 goldens (a few dollars, minutes) — with a `--dry-run` and optional metric subset to bound cost.
- **No application code**: nothing in a future `src/` is touched.
- **Linear**: implements CHO-19; consumes CHO-18, closes the CHO-16→17→18→19 retrieval-eval arc.
