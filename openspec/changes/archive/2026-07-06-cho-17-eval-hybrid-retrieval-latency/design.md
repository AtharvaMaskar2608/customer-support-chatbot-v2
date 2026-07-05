## Context

Eval 2 measures hybrid-retrieval latency over the real `kb_faq` table for the customer-support RAG pipeline. The retrieval substrate already exists: `chunk` text embedded into `embedding vector(1536)` (`text-embedding-3-large` truncated to 1536 dims), a generated `tsv tsvector`, an HNSW `vector_cosine_ops` index, and a GIN index on `tsv`. The corpus is ~1,102 rows and fixed in size. `EXPLAIN ANALYZE` shows the vector query resolves to an exact **seq scan (~7.5 ms)**, not HNSW — correct at this scale. This design covers a new external dependency (a local Postgres, via `asyncpg`) and the measurement methodology; the numeric outcome is largely predictable, so the value is a clean, honest per-stage breakdown rather than discovery.

## Goals / Non-Goals

**Goals:**
- Produce trustworthy per-stage latency: query embed, vector-only, FTS-only, RRF-combined, and end-to-end.
- Isolate **pure retrieval infra latency** from query-embedding latency (embed once, reuse vectors).
- Use realistic queries (sampled from `kb_faq.question`) so full-text matches are genuine.
- Record the actual query plan so the timed path is understood, not assumed.
- Confirm local Postgres holds up under modest concurrency.

**Non-Goals:**
- Re-ranking / cross-encoder (RRF-only — Option A).
- Retrieval quality (recall@k / nDCG / Contextual Precision·Recall — later deepeval eval).
- HNSW tuning, candidate-depth sweeps, or any scale study (moot at fixed ~1.1k rows).
- Any application/`src/` code, and any writes to `kb_faq`.

## Decisions

**D1 — New `evals/retrieval/` sibling, reusing the evals conventions.**
Layout: `evals/retrieval/benchmark_retrieval.py`, `evals/retrieval/rrf.sql` (or inline), `evals/retrieval/results/`. Reuses `.env` loading, warmup-discard, percentile helpers, and CSV+table+plot output patterns from `evals/embeddings/`.

**D2 — `asyncpg` as the driver.**
Fastest async Postgres driver; a connection pool models real serving and enables the concurrency pass. Alternative `psycopg3` — also async but `asyncpg` is the performance pick for a latency benchmark.

**D3 — RRF as a single SQL round-trip.**
Two CTEs (`vec` via `embedding <=> $1::vector(1536)`, `fts` via `ts_rank_cd(tsv, plainto_tsquery('english',$2))`), each `row_number()`-ranked and `LIMIT $N`, then `FULL OUTER JOIN ... USING (id)` and score `1/(k+rank_vec) + 1/(k+rank_fts)`, `ORDER BY rrf LIMIT $K`, `k=60`. RRF fuses by **rank**, so PG's native `ts_rank_cd` ordering feeds it correctly even though it is not BM25. Alternative (two queries + Python merge) adds a round-trip and app-side work — rejected.

**D4 — Isolate pure retrieval by embedding the query set once.**
Pass A embeds the ~100 sampled queries once, caches the vectors, then repeatedly runs the SQL path (vector-only, FTS-only, RRF) to measure infra latency without embed noise. Pass B re-runs end-to-end with live embed to show embed's share (~95%+). This cleanly separates the number the user asked for ("retrieval latency") from the embed already characterized in CHO-16.

**D5 — Query embedding must match the column recipe exactly.**
`text-embedding-3-large` at `dimensions=1536`, cosine. Any mismatch (wrong model, wrong dims, or a `halfvec` cast against a `vector` index) silently degrades results or forces a seq scan on the wrong path. The benchmark asserts vector dimensionality = 1536 before running.

**D6 — Record the query plan, don't assume it.**
The run first executes `EXPLAIN (ANALYZE, FORMAT TEXT)` for the vector and RRF queries and saves the plan alongside results, so "seq scan, exact, ~7.5 ms" is documented for this run rather than inferred. Confirms whether HNSW engages (it should not at this scale).

**D7 — Structured output: CSV + table + plot.**
One CSV row per (pass, arm, concurrency) with p50/p95/p99/mean/max, throughput, rows returned; a printed comparison table; and a small stage-breakdown bar/line plot to `results/`. Timestamps stamped at runtime.

## Risks / Trade-offs

- **The result is predictable (DB ~free, embed dominates).** → That is the point: confirm with real numbers and a recorded plan. Frame outputs as a per-stage breakdown, not a discovery.
- **Sampled queries could echo `chunk` too closely and flatter FTS.** → Sample from `question` (not `chunk`), optionally lightly paraphrase; report token lengths so the query profile is transparent.
- **Local page cache warms after first queries.** → Discard warmup; report warm steady-state (matches a live server).
- **`asyncpg` pool size caps effective concurrency.** → Set pool size ≥ max concurrency tested; record pool config with results.
- **`ts_rank_cd` is not BM25.** → Acceptable: RRF is rank-based, and BM25 scoring is a quality question deferred to the golden-set eval. Note it in findings.
- **DSN / secrets in `.env`.** → Read-only queries only; never log the DSN; `.env` already git-ignored.

## Migration Plan

Additive only — new `evals/retrieval/` tree, `asyncpg` appended to `evals/requirements.txt`. No existing code or the `kb_faq` table is modified. "Rollback" = delete the directory.

## Open Questions

_All resolved._ Concurrency pass (C) is **in** — RRF at concurrency `[1,10,25]` via the pool. The Postgres DSN is a single `DATABASE_URL` read from `.env`. Candidate depth `N=50`, top-K=10, and RRF `k=60` are fixed defaults that do not affect latency materially at this scale and can be overridden by CLI flags if desired.
