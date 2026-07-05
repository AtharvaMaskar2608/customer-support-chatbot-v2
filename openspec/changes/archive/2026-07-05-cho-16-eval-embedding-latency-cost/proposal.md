## Why

Before committing to an embedding model for the customer-support RAG pipeline, we need real numbers on query-time behavior. Knowledge-base ingestion is a one-time, near-negligible cost (single-digit dollars for either OpenAI model), but queries run continuously — so the axis that actually differentiates `text-embedding-3-small` from `text-embedding-3-large` at query time is **latency under concurrency** (and rate-limit headroom), not cost. This benchmark produces the clean, raw baseline that later model-selection decisions rest on. Tracked as CHO-16.

## What Changes

- Add a standalone `evals/` area (separate from future `src/`) for evaluation harnesses, with `evals/embeddings/` as the first inhabitant.
- Add a query-embedding latency & cost benchmark that measures OpenAI `text-embedding-3-small` and `text-embedding-3-large` under varying concurrency.
- Sweep large's output dimensions (3072 / 1536 / 256 via the Matryoshka `dimensions` param) to inform downstream storage/search — while noting embed latency is expected to stay ~flat.
- Control for caching so results reflect the **raw, uncached** model: unique input per call (nonce/pool), keep-alive on, warmup discarded.
- Emit structured results (latency p50/p95/p99/mean/max, throughput, error/timeout/429 rates, $/query and $/1M queries) to `evals/embeddings/results/`.
- Ship with a synthetic pool of ~50 support-style questions (no knowledge-base dependency; real queries swap in later).

Non-goals (deliberately deferred): retrieval quality (needs a golden set), production semantic/exact caching (needs real query traffic), and ingestion-cost measurement (one-time, negligible).

## Capabilities

### New Capabilities
- `embedding-benchmark`: A repeatable harness that benchmarks OpenAI query-embedding models on latency, throughput, reliability, and cost across a model × concurrency × dimensions grid, controlling for caching, and emits structured comparison results.

### Modified Capabilities
<!-- None — greenfield; no existing specs change. -->

## Impact

- **New directory**: `evals/embeddings/` (benchmark script + `results/`), plus a top-level `evals/README.md` describing what each eval measures.
- **Dependencies**: `openai` (embeddings API), `tiktoken` (token counting for cost), and a plotting/reporting lib (e.g. `matplotlib` or CSV-only) — added to an evals-scoped requirements file, not the app.
- **Config**: reads `OPENAI_API_KEY` from `.env`; makes real, billed API calls (cost is trivial — a few cents per full run).
- **No application code**: nothing in a future `src/` is touched; this is an isolated measurement harness.
- **Linear**: implements CHO-16.
