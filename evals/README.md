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
