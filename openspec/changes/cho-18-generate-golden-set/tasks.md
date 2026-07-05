## 1. Scaffold + dependencies

- [x] 1.1 Create `evals/quality/` tree: `generate_goldens.py`, `claude_model.py`, `goldens/`, `__init__.py`
- [x] 1.2 Add `deepeval` and `anthropic` to `evals/requirements.txt`; install into venv
- [x] 1.3 Pin the installed `deepeval` version (`deepeval==4.0.7`); `Synthesizer.__init__` takes `model, async_mode, max_concurrent, filtration_config, evolution_config, styling_config, cost_tracking`. **A native `deepeval.models.AnthropicModel` IS present** (schema-aware `generate`/`a_generate`), so we prefer it over a hand-rolled wrapper. Evolutions off via `EvolutionConfig(num_evolutions=0)`; quality filter is `FiltrationConfig(synthetic_input_quality_threshold=0.5)`.

## 2. Claude model wiring

- [x] 2.1 Implement `ClaudeModel(DeepEvalBaseLLM)` in `claude_model.py` (model `claude-sonnet-5`): `generate` / `a_generate` / `get_model_name`, reading `ANTHROPIC_API_KEY` from `.env`
- [x] 2.2 Prefer the native Anthropic model class if the installed `deepeval` ships one; otherwise use the wrapper (`make_claude_model` → resolved to `native` here)
- [x] 2.3 Verify wiring with a single `generate()` call before any bulk generation (returned `OK`)

## 3. Sampling + contexts

- [x] 3.1 Connect to Postgres via `asyncpg` (`DATABASE_URL`/`PGPASSWORD` from `.env`); `SELECT id, topic, question, answer, chunk FROM kb_faq WHERE embedding IS NOT NULL`
- [x] 3.2 Sample ~250 distinct chunks stratified by topic (proportional across the 18 topics) — largest-remainder allocation, ≥1/topic, capped at availability
- [x] 3.3 Build `contexts = [[chunk]]` and a `context_index → kb_faq.id` map (via robust chunk-text→id lookup, since goldens return in completion order)

## 4. Synthetic goldens

- [x] 4.1 Construct `Synthesizer(model=<native AnthropicModel claude-sonnet-5>)`
- [x] 4.2 `generate_goldens_from_contexts(contexts=..., max_goldens_per_context=1)` with evolutions OFF (`EvolutionConfig(num_evolutions=0)`); added `StylingConfig` to force real-user paraphrasing (design R2 mitigation)
- [x] 4.3 Attach `chunk_id` to each golden via chunk-text→id map; record `synthetic_input_quality`
- [x] 4.4 Dry run (5 chunks): queries are natural reworded paraphrases (not verbatim `Q:`), answerable from their chunk ✓

## 5. Raw-question baseline

- [x] 5.1 Build a ~50-golden baseline directly from real `kb_faq.question` (input=question, expected_output=answer, chunk_id=id) — 50 goldens, topic-stratified
- [x] 5.2 Save it as a separate dataset from the synthetic goldens (`baseline_goldens_*.json`)

## 6. Output + verify

- [x] 6.1 Save both datasets as timestamped JSON in `evals/quality/goldens/` with `input`, `expected_output`, `context`, `chunk_id` (+ `topic`, `synthetic_input_quality`, `meta`)
- [x] 6.2 Sanity: every golden has a valid `chunk_id` in `kb_faq` ✓; dataset round-trips (load → same count) ✓
- [x] 6.3 Topic coverage proportional across all 18 topics (within ±0.3pp) ✓; 0 contexts dropped; 35/250 goldens below the 0.5 quality bar (kept, scored) → 215 usable ≥0.5
- [x] 6.4 Update CHO-18 with counts, topic coverage, sample goldens, and both dataset paths (posted as a comment)
