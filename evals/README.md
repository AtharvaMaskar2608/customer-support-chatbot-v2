# evals/

Evaluation harnesses for the customer-support RAG pipeline. These are standalone
scripts (not imported by the app) with their own dependencies. They run as
measurements, not pass/fail unit tests.

```
evals/
├── requirements.txt          # eval-scoped deps (openai, tiktoken, dotenv, matplotlib)
└── embeddings/
    ├── benchmark_embeddings.py   # entry point
    ├── config.py                 # models, grid, trials, prices, caps
    ├── queries.py                # synthetic support-style query pool
    └── results/                  # timestamped CSV + PNG per run (gitignored)
```

## Setup

```bash
python -m venv venv && source venv/bin/activate   # if not already
pip install -r evals/requirements.txt
# OPENAI_API_KEY is read from the repo-root .env
```

## Eval 1 — Query-embedding latency & cost (CHO-16)

**Question:** which embedding model — `text-embedding-3-small` vs
`text-embedding-3-large` — to use for query-time embedding, judged on latency
under concurrency (cost is a rounding error either way).

**What it measures:** raw, uncached per-call latency (p50/p95/p99/mean/max),
throughput, reliability (error/timeout/429 counts), and cost ($/query, $/1M
queries) across a `model × dims × concurrency` grid. Caching is controlled for:
unique input per call (nonce), warmup discarded, keep-alive on.

```bash
# Cheap wiring check first (1 model, concurrency 1, ~5 calls):
python -m evals.embeddings.benchmark_embeddings --dry-run

# Full grid (real, billed calls — a few cents total):
python -m evals.embeddings.benchmark_embeddings
```

Outputs land in `evals/embeddings/results/` as `embedding_latency_<UTC>.csv`
and `embedding_latency_<UTC>.png`. Absolute latencies are comparative (same
machine/run), not absolute SLAs.

### Findings — first full run (2026-07-05 UTC)

20 configs × 100 trials = 2,200 calls. **0 errors, 0 timeouts, 0 rate-limit (429)
rejections.** Single run; absolute tails vary run-to-run (see caveat).

1. **Median latency is fast and concurrency-insensitive.** p50 stays ~260–410 ms
   for every config regardless of load — the *typical* query embed is cheap on
   both models.
2. **Tail latency is the whole story.** p95/p99 explode past ~5–10 concurrent:
   at concurrency 25–50, p95/p99 reach **5–15 seconds**. Whatever embedding model
   you pick, running many *simultaneous* query embeds is user-hostile.
3. **429s never fired.** Congestion showed up purely as latency, not rejection,
   at these levels for this account/region. So the ceiling is a *latency* wall,
   not a hard rate limit here.
4. **Dimensions: truncating `large` is basically free on latency.** `large@3072`
   carries a consistent **~130 ms p50 penalty** over the truncated variants;
   `large@1536` ≈ `large@256` ≈ `small@1536` on p50. Truncating large to 1536
   dims keeps large's quality headroom at small-like latency and smaller vectors.
5. **`small@1536` had the worst tail** in this run (hit the ~15 s ceiling by
   concurrency 10–25) while large's variants stayed lower (~5–10 s). Treat this
   comparison cautiously — small ran first, so run-order/network variance is a
   confound (see caveat).
6. **Cost is a rounding error, as predicted.** small ≈ **$0.19 / 1M queries**,
   large ≈ **$1.22 / 1M queries** (avg ~9 tokens/query). Not a deciding factor.

**Implications**
- Choose the query-embedding model on **latency + quality**, not cost.
- Keep *real* query-embedding concurrency low (≤ ~5–10) and/or add a bounded
  queue plus a query-embedding cache; tails past that are measured in seconds.
- If picking `large` for retrieval quality, **truncate to 1536 dims** — no
  latency penalty vs 3072, and half the vector footprint.

**Caveat / good follow-up:** this is one run and models were tested in sequence,
so the cross-model tail comparison carries run-order bias. Re-run with models
**interleaved per concurrency level** (and ideally a couple of repeats) before
treating the small-vs-large tail difference as settled.

Artifacts: `results/embedding_latency_20260705T173204Z.csv` and `.png`.

## Eval 2 — Hybrid retrieval (vector + FTS + RRF) latency (CHO-17)

**Question:** where does the latency of RRF-only hybrid retrieval over `kb_faq`
(~1,102 rows, `vector(1536)`, local Postgres) actually go? Three passes: (A) pure
retrieval with the query set embedded once and reused, (B) end-to-end with live
embed, (C) RRF under concurrency. Read-only. RRF-only — no re-ranker.

```bash
python -m evals.retrieval.benchmark_retrieval --dry-run   # wiring check
python -m evals.retrieval.benchmark_retrieval             # full run
```

Requires `DATABASE_URL` (+ `PGPASSWORD`) and `OPENAI_API_KEY` in `.env`. Outputs a
timestamped CSV, a stage-breakdown + concurrency plot, and the captured
`EXPLAIN ANALYZE` plan under `evals/retrieval/results/`.

### Findings — first full run (2026-07-06 UTC)

100 queries sampled from `kb_faq.question`, 100 trials/arm. p50 / p95 / p99 (ms):

| pass | arm | p50 | p95 | p99 | q/s |
|---|---|---|---|---|---|
| A pure | vector | 51.9 | 76.4 | 96.7 | 18.0 |
| A pure | fts | 59.2 | 131.3 | 201.8 | 13.9 |
| A pure | rrf | 55.1 | 76.4 | 120.8 | 17.6 |
| B e2e | embed+rrf | 352.9 | 871.6 | 4361.1 | 1.8 |
| C conc=10 | rrf | 67.1 | 222.5 | 240.1 | 96.9 |
| C conc=25 | rrf | 111.3 | 1110.6 | 1113.3 | 81.4 |

1. **Seq scan confirmed, and it's fast — server-side ~5.5 ms, exact** (from the
   captured `EXPLAIN ANALYZE`). HNSW never engaged; correct at this scale.
2. **Correction to the prediction: retrieval from Python is ~50 ms p50, not
   ~8–12 ms.** The seq scan is ~6 ms server-side; the extra ~45 ms is
   **client-side** — shipping the 1536-dim query vector as a ~15 KB *text* literal
   + round-trip + asyncpg parsing, every call. **Actionable:** register an asyncpg
   binary `vector` codec (or prepared statements) to cut most of that overhead if
   retrieval latency ever matters.
3. **RRF fusion is free** — `rrf` (55 ms) ≈ `vector` (52 ms). The FULL OUTER JOIN
   over two ~50-row lists adds nothing. `fts` is slightly slower with a fatter
   tail (p99 202 ms).
4. **End-to-end is dominated by the embed** — ~300 ms of the 353 ms p50 (~85%),
   with a brutal tail (p99 4.4 s). Reaffirms CHO-16: the query embed is the lever,
   and a query-embed cache is the real optimization — not anything in the DB.
5. **Concurrency: local PG scales to ~97 q/s at concurrency 10** (p99 240 ms),
   then saturates — at 25 concurrent, throughput *drops* to 81 q/s and p99 blows
   to ~1.1 s. Each query is a CPU-bound seq scan, so concurrency competes for
   cores. Keep effective retrieval concurrency ≤ ~10.

**Implications**
- Retrieval infra is fast but not *free* from Python (~50 ms), and most of that is
  vector-literal serialization — fix with a binary codec if it matters.
- The dominant latency lever remains the query embed (cache it).
- Cap concurrent retrieval at ~10 for healthy tails.

Artifacts: `results/retrieval_latency_20260705T192224Z.csv`, `.png`, and
`results/retrieval_plan_20260705T192224Z.txt`.
