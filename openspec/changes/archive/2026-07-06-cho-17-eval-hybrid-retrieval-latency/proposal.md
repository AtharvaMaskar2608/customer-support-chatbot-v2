## Why

The second RAG-pipeline eval: measure the latency of hybrid retrieval — dense vector + full-text + Reciprocal Rank Fusion (RRF), all in Postgres — against the real `kb_faq` table, driven from Python. We need concrete per-stage numbers to confirm where the retrieval latency budget actually goes before building the agent on top. Tracked as CHO-17. Re-ranking is deliberately excluded (RRF-only).

Grounding facts already verified: `kb_faq` is ~1,102 rows and will not grow substantially; `embedding` is `vector(1536)` (`text-embedding-3-large` truncated to 1536 dims) with an HNSW `vector_cosine_ops` index and a GIN index on generated `tsv`; the DB is local (`localhost:5433`); and `EXPLAIN ANALYZE` shows the planner correctly using an exact **seq scan (~7.5 ms)**, not HNSW, at this scale. This makes the eval a **confirmation** of the per-stage breakdown, not a tuning study — the expected verdict is that the DB side is ~8–12 ms and the query embed (~280 ms, from CHO-16) is the entire latency lever.

## What Changes

- Add `evals/retrieval/` (sibling of `evals/embeddings/`) housing a hybrid-retrieval latency benchmark.
- Add `asyncpg` (+ config) to connect to the local Postgres and query `kb_faq`.
- Implement RRF as a single SQL round-trip fusing the HNSW-backed vector arm and the GIN-backed full-text arm.
- Sample the query set from real `kb_faq.question` values (genuine full-text matches), embedding each with `text-embedding-3-large` at `dimensions=1536` to match the column.
- Measure three passes: (A) **pure retrieval** (embed once, reuse vectors, isolate the SQL path: vector-only, FTS-only, RRF-combined), (B) **end-to-end** (live embed + RRF), (C) optional **DB concurrency** (1/10/25 concurrent RRF queries).
- Emit per-stage latency (p50/p95/p99), a comparison table, and a results artifact under `evals/retrieval/results/`.
- Verify the index/plan reality first (record the `EXPLAIN ANALYZE` plan so the timed path is understood, not assumed).

Non-goals (deferred): re-ranking / cross-encoder; retrieval quality (recall@k / nDCG / Contextual Precision·Recall — the deepeval golden-set eval); HNSW tuning or any scale study (moot at fixed ~1.1k rows).

## Capabilities

### New Capabilities
- `hybrid-retrieval-benchmark`: A repeatable harness that measures per-stage latency of RRF-only hybrid retrieval (vector + full-text + RRF) over `kb_faq` from Python — isolating pure retrieval latency from query-embedding latency, sampling real queries, recording the actual query plan, and emitting structured comparison results.

### Modified Capabilities
<!-- None. `embedding-benchmark` is unaffected; this is a new sibling capability. -->

## Impact

- **New directory**: `evals/retrieval/` (benchmark script, RRF SQL, `results/`).
- **Dependencies**: `asyncpg` added to `evals/requirements.txt`; reuses `openai`, `tiktoken`, `python-dotenv`, `matplotlib` already present.
- **Config/secrets**: reads `OPENAI_API_KEY` and a Postgres DSN (`host=localhost port=5433 dbname=customer-support-chatbot`) from `.env` (already git-ignored). Read-only against `kb_faq` — no writes to the KB.
- **External calls**: ~100 embedding calls per run (negligible cost) + local, read-only DB queries.
- **No application code**: nothing in a future `src/` is touched.
- **Linear**: implements CHO-17; builds on CHO-16 findings.
