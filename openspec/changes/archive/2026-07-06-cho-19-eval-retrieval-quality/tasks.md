## 1. Scaffold + API de-risk

- [x] 1.1 Create `evals/quality/eval_retrieval.py` and `evals/quality/results/` (with `.gitkeep`); gitignore `evals/quality/results/*`
- [x] 1.2 DeepEval retriever metrics take `model=`; required `LLMTestCase` fields — Recall/Precision: input+retrieval_context+expected_output; Relevancy: input+retrieval_context. None need `actual_output`.
- [x] 1.3 Pass the Claude model via `model=make_claude_model(...)[0]`; `LLMTestCase` constructs WITHOUT `actual_output`; verified a live `ContextualRelevancy.measure` = 1.0 with the native model.

## 2. Data loading

- [x] 2.1 Load the latest synthetic and baseline golden JSON from `evals/quality/goldens/` (each: `input`, `expected_output`, `context`, `chunk_id`, `topic`, `synthetic_input_quality`)
- [x] 2.2 Support `--min-quality` to filter synthetic goldens by `synthetic_input_quality`; report the excluded count
- [x] 2.3 Support `--dry-run` (few goldens), `--limit N`, and `--metrics` subset selection

## 3. Retrieval path (reuse CHO-17)

- [x] 3.1 Import `SQL_RRF` (+ arm SQL) and `config` (EMBED_MODEL/DIMS, N_CANDIDATES, TOP_K, RRF_K) from `evals.retrieval`; connect via `asyncpg` (`DATABASE_URL`/`PGPASSWORD` from `.env`)
- [x] 3.2 Assert `vector_dims(kb_faq.embedding) == EMBED_DIMS` before scoring
- [x] 3.3 Embed each golden `input` (`text-embedding-3-large`@1536) and run `SQL_RRF` to get the ordered top-K id list
- [x] 3.4 Fetch the top-K chunk texts (`WHERE id = ANY($1)`, re-ordered to RRF rank) as `retrieval_context`

## 4. Deterministic chunk-id metrics

- [x] 4.1 For each golden compute rank of ground-truth `chunk_id`, `recall@TOP_K`, `hit@k` (k=1,3,5,10), and MRR
- [x] 4.2 Aggregate overall and per-topic (mean recall/hit@k/MRR)

## 5. DeepEval LLM metrics

- [x] 5.1 Build `LLMTestCase(input, actual_output=expected_output, expected_output, retrieval_context)` per golden
- [x] 5.2 Score ContextualRecall / ContextualPrecision / ContextualRelevancy with Claude (`claude-sonnet-5`); cap concurrency (~8, generous timeout) per the CHO-18 lesson
- [x] 5.3 Verify each metric on one test case before the full run

## 6. Compare + output

- [x] 6.1 Run both datasets through the identical path; compute synthetic recall, baseline recall, and the circularity gap (baseline − synthetic)
- [x] 6.2 Save per-golden records to timestamped JSON and an aggregate + per-topic summary to CSV under `evals/quality/results/`
- [x] 6.3 Print a compact table: synthetic vs baseline recall/hit@k/MRR + the three LLM-metric means + circularity gap; log below-0.5 synthetic count
- [x] 6.4 Dry run first (5 goldens) to eyeball retrieved contexts and metric wiring before the full run
- [x] 6.5 Created CHO-19 (didn't exist) with the recall table, circularity gap, per-topic coverage, and result paths
