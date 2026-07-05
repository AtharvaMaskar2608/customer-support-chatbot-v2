## Context

The customer-support chatbot is an agentic AI + RAG system. Its first evaluation step is choosing an embedding model. The repo is greenfield (no `src/`, empty venv). We measure query-time behavior of OpenAI `text-embedding-3-small` vs `text-embedding-3-large` because ingestion is a one-time, negligible cost while queries are continuous — making **latency under concurrency** and **rate-limit headroom** the deciding factors, with cost falling out for free as tokens × rate. Tracked as CHO-16. This design covers a new external dependency (OpenAI API) and a measurement harness whose correctness hinges on caching/warmup controls — enough non-obvious decisions to warrant a design doc.

## Goals / Non-Goals

**Goals:**
- Produce trustworthy, **raw uncached** latency/throughput/reliability/cost numbers per model.
- Vary model × concurrency (`1,5,10,25,50`) and large's output dims (`3072,1536,256`).
- Surface where each model hits rate limits (429s) and how tail latency (p99) degrades under load.
- Emit machine-readable results plus a human-readable summary, reproducibly.

**Non-Goals:**
- Retrieval/answer quality (needs a golden set — later change).
- Production semantic/exact caching (needs real traffic — later change).
- Ingestion-cost measurement (one-time, negligible).
- Any application/`src/` code.

## Decisions

**D1 — Standalone `evals/` tree, not inside `src/`.**
Evals are a parallel concern with their own deps and run as scripts, not imports. Layout: `evals/embeddings/benchmark_embeddings.py`, `evals/embeddings/results/`, `evals/embeddings/queries.py` (synthetic pool), `evals/README.md`, `evals/requirements.txt`.
*Alternative:* `tests/` — rejected; these are measurements, not pass/fail unit tests.

**D2 — Defeat caching by making every call embed unique text.**
Reuse of identical inputs risks input-level cache hits (OpenAI-side behavior is undocumented) that deflate p95. Each call appends a per-call nonce (e.g. a counter) to a base query; latency tracks token count, not content, so a suffix is safe. Keep HTTP keep-alive **on** and discard warmup calls — that's connection warmth (which prod also has), not a response cache.
*Alternative:* trust that OpenAI doesn't cache — rejected; too load-bearing to assume.

**D3 — Concurrency via a bounded async client (`asyncio` + `AsyncOpenAI`).**
Launch N in-flight requests per concurrency level using a semaphore of size N; time each call individually. This mirrors production parallelism and exposes 429s and p99 blow-up that single-call tests hide.
*Alternative:* thread pool — workable but async is the natural fit for I/O-bound API calls and cleaner backpressure.

**D4 — Cost computed locally from `tiktoken`, not from API usage fields.**
Count query tokens with `tiktoken` (`cl100k_base`) and multiply by per-model list price. Deterministic and offline-checkable. Prices live in a small config dict at the top of the script, using OpenAI's **synchronous** per-1M-token list prices: `text-embedding-3-small = $0.02`, `text-embedding-3-large = $0.13`. The Batch API is ~half price but is async (up to 24h) and thus irrelevant to live query embedding — we deliberately do **not** use batch prices here.

**D5 — Dims sweep applies to `large` only, with an explicit expectation.**
`dimensions` truncates via Matryoshka after full compute, so embed latency is expected to be ~flat across `3072/1536/256`; the value is downstream (storage/search). We still record it so a flat result is documented, not assumed. `small` is run at its native dim only.

**D6 — Results are structured + summarized: CSV + plot.**
Write one CSV row per `(model, dims, concurrency)` config to `results/` with all metrics (CSV is the source of truth), plus a printed comparison table. Timestamps are stamped by the script at runtime. Also emit a matplotlib **latency-vs-concurrency plot** (p50/p95/p99 lines per model) to `results/` — confirmed in scope for the first pass.

**D7 — Statistical validity.**
~100 timed trials per config after warmup; report p50/p95/p99/mean/max so tails are visible, not just averages.

## Risks / Trade-offs

- **Network/region variance skews absolute latency** → Report percentiles over ~100 trials; fix region; treat numbers as comparative (small vs large on the same machine/run), not absolute SLAs.
- **Rate limits (429s) cap high-concurrency runs, especially for `large`** → Treat 429 rate as a *measured output*, not a failure; use bounded retry with jitter but **record** throttling rather than hiding it. This is a genuine differentiator we want to see.
- **Undocumented OpenAI-side caching could still leak through** → Unique-per-call nonce (D2) is the primary defense; if suspiciously low latencies appear, increase nonce entropy.
- **Real billed API calls** → Cost is trivial (cents/run) but the run needs a valid `OPENAI_API_KEY`; guard against accidental huge loops with a hard cap on total calls.
- **`dimensions` support/behavior may differ from expectation** → D5 records the result regardless; if latency *does* move, that's a finding, not a bug.

## Migration Plan

Additive only — new `evals/` tree, no existing code touched. "Rollback" = delete the directory. Deps go in an evals-scoped `requirements.txt`, keeping the (future) app environment clean.

## Open Questions

_All resolved:_
- ~~Per-model list prices~~ → confirmed: small `$0.02` / large `$0.13` per 1M tokens, synchronous (D4).
- ~~Results format~~ → confirmed: CSV (source of truth) **+** matplotlib latency-vs-concurrency plot (D6).
