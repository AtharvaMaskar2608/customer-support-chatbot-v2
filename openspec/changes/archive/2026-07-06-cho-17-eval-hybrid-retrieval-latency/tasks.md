## 1. Scaffold retrieval eval

- [x] 1.1 Create `evals/retrieval/` tree: `benchmark_retrieval.py`, `results/`, `__init__.py`
- [x] 1.2 Add `asyncpg` to `evals/requirements.txt` and install into venv
- [x] 1.3 Add Postgres DSN handling: read a single `DATABASE_URL` (e.g. `postgresql://atharva@localhost:5433/customer-support-chatbot`) from `.env` (never log the DSN)
- [x] 1.4 Add config: candidate depth `N=50`, top-K=10, RRF `k=60`, trials, warmup, concurrency levels `[1,10,25]`, pool size ≥ max concurrency

## 2. DB connection & query set

- [x] 2.1 Set up an `asyncpg` connection pool (size ≥ max concurrency); warm and discard warmup
- [x] 2.2 Assert `pgvector` is present and `kb_faq.embedding` is `vector(1536)`; capture `SELECT count(*)`
- [x] 2.3 Sample ~100 queries from real `kb_faq.question` values; record their token-length profile
- [x] 2.4 Embed the query set once with `text-embedding-3-large` at `dimensions=1536`; assert dim==1536; cache the vectors

## 3. Retrieval queries

- [x] 3.1 Implement the vector-only arm: `ORDER BY embedding <=> $1::vector(1536) LIMIT N`
- [x] 3.2 Implement the full-text-only arm: `ts_rank_cd(tsv, plainto_tsquery('english',$1)) ... WHERE tsv @@ ... LIMIT N`
- [x] 3.3 Implement the RRF-combined query as a single SQL round-trip (two CTEs, `row_number()`, `FULL OUTER JOIN USING (id)`, score `1/(k+rank)` summed, `ORDER BY rrf LIMIT K`)
- [x] 3.4 Record `EXPLAIN (ANALYZE, FORMAT TEXT)` for the vector and RRF queries; save plan text; note index-scan vs seq-scan

## 4. Measurement passes

- [x] 4.1 Pass A (pure retrieval): reuse cached vectors, time vector-only / FTS-only / RRF-combined separately
- [x] 4.2 Pass B (end-to-end): time live embed + RRF together to expose the embed share
- [x] 4.3 Pass C (concurrency): run RRF at concurrency `[1,10,25]` via the pool
- [x] 4.4 Compute latency `p50/p95/p99/mean/max` (ms) and throughput (q/s) per measured unit

## 5. Output

- [x] 5.1 Write one record per (pass, arm, concurrency) to a timestamped CSV in `evals/retrieval/results/`
- [x] 5.2 Print a human-readable comparison table across passes/arms
- [x] 5.3 Emit a stage-breakdown plot (embed vs vector vs FTS vs RRF; and latency vs concurrency) to `results/`
- [x] 5.4 Save the captured query-plan text alongside the results

## 6. Verify and record

- [x] 6.1 Dry run: a few queries, confirm DB connection, dim assertion, and plan capture wiring
- [x] 6.2 Full run; sanity-check that the vector arm plan matches expectation (seq scan, exact) and RRF returns sane top-K
- [x] 6.3 Capture findings (per-stage breakdown; DB ≈ negligible; embed dominates) in `evals/README.md` or a results note
- [x] 6.4 Update CHO-17 with the results summary and link the results file
