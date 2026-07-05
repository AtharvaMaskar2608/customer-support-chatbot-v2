## 1. Scaffold evals area

- [x] 1.1 Create `evals/` tree: `evals/embeddings/`, `evals/embeddings/results/`, `evals/README.md`
- [x] 1.2 Add `evals/requirements.txt` with `openai`, `tiktoken`, and (optional) `matplotlib`; install into venv
- [x] 1.3 Add `evals/README.md` describing what each eval measures and how to run this one
- [x] 1.4 Ensure `.env` is git-ignored and `OPENAI_API_KEY` loads (e.g. via `python-dotenv`); add `results/` output convention

## 2. Query set and config

- [x] 2.1 Create `evals/embeddings/queries.py` with a synthetic pool of ~50 support-style questions
- [x] 2.2 Implement per-call nonce so every embedding input is unique (defeats input-level caching)
- [x] 2.3 Define config: models (`text-embedding-3-small`, `text-embedding-3-large`), concurrency `[1,5,10,25,50]`, large dims `[3072,1536,256]`, trials/config (~100), warmup count, hard cap on total calls
- [x] 2.4 Add per-model price dict for cost math (synchronous list prices: small `$0.02`, large `$0.13` per 1M tokens)

## 3. Benchmark core

- [x] 3.1 Set up `AsyncOpenAI` client with keep-alive on and a configurable timeout
- [x] 3.2 Implement single-call timing (wall-clock per request) with error/timeout/429 classification
- [x] 3.3 Implement concurrency runner: semaphore of size N keeps up to N requests in flight per config
- [x] 3.4 Run warmup calls per config and discard their timings
- [x] 3.5 Iterate the full grid `(model × dims × concurrency)`, ~100 timed trials each, respecting the total-call cap
- [x] 3.6 Use bounded retry with jitter on 429/transient errors, but record the raw 429 count as a measured output

## 4. Metrics and cost

- [x] 4.1 Compute latency `p50/p95/p99/mean/max` (ms) per config
- [x] 4.2 Compute throughput (embeddings/sec) per config
- [x] 4.3 Compute reliability counters: error rate, timeout rate, 429 count
- [x] 4.4 Compute cost via `tiktoken` token counts × per-model price → `$/query` and `$/1M queries`

## 5. Output

- [x] 5.1 Write one record per `(model, dims, concurrency)` to a timestamped CSV/JSON in `evals/embeddings/results/`
- [x] 5.2 Print a human-readable comparison table across all configs
- [x] 5.3 Emit a latency-vs-concurrency plot (p50/p95/p99 lines per model) via matplotlib to `results/`

## 6. Verify and record

- [x] 6.1 Do a small dry run (1 model, concurrency 1, few trials) to confirm wiring and cost cap
- [x] 6.2 Run the full grid; sanity-check that unique inputs + warmup produce plausible (non-cached) p95s
- [x] 6.3 Capture findings (small vs large: latency under load, 429 onset, cost) in `evals/README.md` or a results note
- [x] 6.4 Update CHO-16 with the results summary and link the results file
