## 1. Scaffold + dependencies

- [ ] 1.1 Create `evals/quality/` tree: `generate_goldens.py`, `claude_model.py`, `goldens/`, `__init__.py`
- [ ] 1.2 Add `deepeval` and `anthropic` to `evals/requirements.txt`; install into venv
- [ ] 1.3 Pin the installed `deepeval` version; print the `Synthesizer.__init__` signature and check for a native Anthropic model class

## 2. Claude model wiring

- [ ] 2.1 Implement `ClaudeModel(DeepEvalBaseLLM)` in `claude_model.py` (model `claude-sonnet-5`): `generate` / `a_generate` / `get_model_name`, reading `ANTHROPIC_API_KEY` from `.env`
- [ ] 2.2 Prefer the native Anthropic model class if the installed `deepeval` ships one; otherwise use the wrapper
- [ ] 2.3 Verify wiring with a single `generate()` call before any bulk generation

## 3. Sampling + contexts

- [ ] 3.1 Connect to Postgres via `asyncpg` (`DATABASE_URL`/`PGPASSWORD` from `.env`); `SELECT id, topic, question, answer, chunk FROM kb_faq WHERE embedding IS NOT NULL`
- [ ] 3.2 Sample ~250 distinct chunks stratified by topic (proportional across the 18 topics)
- [ ] 3.3 Build `contexts = [[chunk]]` and a `context_index → kb_faq.id` map

## 4. Synthetic goldens

- [ ] 4.1 Construct `Synthesizer(model=ClaudeModel("claude-sonnet-5"))`
- [ ] 4.2 `generate_goldens_from_contexts(contexts=..., max_goldens_per_context=1)` with evolutions OFF
- [ ] 4.3 Attach `chunk_id` to each golden via the id map; record quality scores
- [ ] 4.4 Dry run first (5 chunks): eyeball that queries are paraphrases (not verbatim `Q:`), answerable from their chunk

## 5. Raw-question baseline

- [ ] 5.1 Build a ~50-golden baseline directly from real `kb_faq.question` (input=question, expected_output=answer, chunk_id=id)
- [ ] 5.2 Save it as a separate dataset from the synthetic goldens

## 6. Output + verify

- [ ] 6.1 Save both datasets as timestamped JSON in `evals/quality/goldens/` with `input`, `expected_output`, `context`, `chunk_id`
- [ ] 6.2 Sanity: every golden has a valid `chunk_id` in `kb_faq`; dataset round-trips (load → same count)
- [ ] 6.3 Check topic coverage of the synthetic set is roughly proportional across the 18 topics; log the quality-filter drop count
- [ ] 6.4 Update CHO-18 with counts, topic coverage, sample goldens, and both dataset paths
