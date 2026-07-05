## Context

The retrieval substrate is done and measured for speed: `kb_faq` (~1,102 rows, 18 topics) with `text-embedding-3-large`@1536 vectors and a Postgres RRF query (dense vector + FTS + Reciprocal Rank Fusion) from CHO-17, and the CHO-18 golden sets — 250 synthetic paraphrased queries and a 50-query raw-question baseline, each carrying a ground-truth `chunk_id`. What's missing is the *quality* verdict: given a realistic query, does RRF actually return the chunk the query came from, and how well is it ranked? This change wires the goldens through the CHO-17 retriever and scores the result. It consumes two existing deps (`deepeval`, `anthropic`) already installed for CHO-18 and reuses the RRF SQL + embedding recipe verbatim, so the eval measures the *real* pipeline, not a reimplementation.

## Goals / Non-Goals

**Goals:**
- Measure whether the CHO-17 RRF pipeline retrieves each golden's ground-truth `chunk_id`, with deterministic recall / hit@k (k=1,3,5,10) / MRR.
- Add DeepEval reference-based retriever metrics (ContextualRecall, ContextualPrecision, ContextualRelevancy) scored by Claude, reusing the CHO-18 `AnthropicModel` wiring.
- Report **synthetic recall vs raw-question baseline recall side by side** to quantify the circularity gap.
- Report per-topic recall so weak areas are visible.
- Reproducible, cost-bounded (dry-run, metric subset, limit), read-only against `kb_faq`.

**Non-Goals:**
- Generator/answer-quality metrics (AnswerRelevancy, Faithfulness) — need an agent generator.
- Cross-encoder / re-ranker; any change to the retriever, RRF constants, or `kb_faq`.
- Tuning N_CANDIDATES / TOP_K / RRF_K — this eval *measures* the current config; tuning is a later change informed by these numbers.

## Decisions

**D1 — Two metric families; the deterministic one is primary.**
The golden `chunk_id` is objective ground truth, so recall / hit@k / MRR are computed directly from the retrieved id list — cheap, reproducible, no LLM, no judge variance. The DeepEval LLM metrics (Claude-judged) are secondary/corroborating: they assess *textual* relevance of the retrieved contexts and catch cases where the right chunk is retrieved but buried, or where near-duplicate chunks are also relevant. If the two families disagree, the `chunk_id` metric wins for the headline recall number.

**D2 — Reuse the CHO-17 RRF SQL and embedding recipe, don't reimplement.**
Import the `SQL_RRF` (and arm SQL) constants and `config` (EMBED_MODEL/DIMS, N_CANDIDATES, TOP_K, RRF_K) from `evals.retrieval` so the eval hits the exact timed path. Query embedding uses `text-embedding-3-large` truncated to 1536 dims; assert the dimension matches `kb_faq.embedding` before running.

**D3 — Per-golden retrieval → test case.**
For each golden: embed `input` once → run `SQL_RRF` → get ordered top-K `id`s → fetch those chunks' text (single `WHERE id = ANY($1)` round trip, re-ordered to RRF rank) as `retrieval_context`. Build a DeepEval `LLMTestCase(input, actual_output, expected_output, retrieval_context)`. Retriever metrics don't score `actual_output`, but `LLMTestCase` requires it — set it to `expected_output` as an inert placeholder (verified in task 1).

**D4 — chunk_id metrics from the ordered id list.**
`rank = index of ground-truth chunk_id in the top-K RRF ids (1-based) or ∞`. `hit@k = rank ≤ k`; `recall@K = hit@TOP_K`; `MRR = 1/rank` (0 if absent). Aggregate as means over the dataset and grouped by topic.

**D5 — Evaluate both datasets; circularity gap is the headline.**
Run the synthetic set and the raw-question baseline through the identical path. Report `baseline recall − synthetic recall` as the circularity gap — the whole reason CHO-18 synthesized paraphrases. Baseline recall is expected to be near-ceiling (the query is verbatim inside its chunk → exact FTS + near-identical vector); synthetic recall is the honest number.

**D6 — Cost controls, mirroring CHO-18's hard-won lessons.**
LLM metrics cost ≈ (#goldens × #metrics) Claude calls. Bound with: `--dry-run` (few goldens), `--limit N`, `--metrics` subset (default all three; `chunk_id`-only path needs zero LLM calls), and a modest DeepEval concurrency cap (CHO-18 showed the default 100 trips Anthropic timeouts → cap at ~8, generous per-request timeout). The deterministic `chunk_id` metrics always run (free), so a zero-LLM smoke eval is always available.

**D7 — Optional quality gate on synthetic goldens.**
`--min-quality` (default 0.0 = include all) filters synthetic goldens by `synthetic_input_quality`. Report the headline synthetic recall on the full set and note how many goldens fall below 0.5, so the reader can see the honest-vs-cleaned split without hiding data.

**D8 — Structured, reproducible output.**
Per-golden records (query, ground-truth chunk_id, retrieved ids, rank, hit flags, metric scores) to timestamped JSON; an aggregate + per-topic summary to CSV, both under `evals/quality/results/` (gitignored, `.gitkeep` tracked). Print a compact table: synthetic vs baseline recall/hit@k/MRR + the three LLM metric means + the circularity gap.

## Risks / Trade-offs

- **DeepEval retriever-metric API / required-field drift.** → First task instantiates each metric, prints its constructor signature and the `LLMTestCase` fields it reads, and runs one metric on one case before the full loop (same de-risking pattern that saved CHO-18).
- **LLM-judge cost/time (~900 calls at full breadth).** → dry-run + limit + metric subset + concurrency cap; deterministic metrics need no LLM at all.
- **Embedding recipe mismatch** (wrong model/dims → meaningless recall). → assert `vector_dims(kb_faq.embedding) == EMBED_DIMS` and the same model string as CHO-16/17 before running.
- **Baseline recall ≈ 100%** could look like a bug. → it is the *expected* circular result and the point of the comparison; documented in the output notes.
- **FTS arm empty for heavily reworded queries.** → RRF `FULL OUTER JOIN` still returns the vector arm; recall degrades gracefully, which is itself a signal worth seeing.
- **Judge variance in the LLM metrics.** → treated as secondary; the deterministic `chunk_id` recall is the reproducible headline, LLM metrics are corroborating color.
- **Ground-truth is single-chunk** (a query may be answerable from sibling chunks too). → recall against the *source* chunk is a strict lower bound; acceptable for v1, noted as a known conservative bias.
