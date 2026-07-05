## Context

CHO-18 builds the golden set that unlocks retrieval-quality evaluation. The `kb_faq` table (~1,102 rows, 18 topics) stores each FAQ as a `chunk` of the form `"{topic} › {section}\nQ: {question}\nA: {answer}"` — the question is verbatim inside the chunk, so real questions can't be used as eval queries without making retrieval circular. The retrieval substrate (embeddings, RRF query) already exists from CHO-16/CHO-17; this change only produces the evaluation dataset. It introduces two new external dependencies (`deepeval`, `anthropic`) and a non-obvious methodology (synthesize-don't-reuse, evolutions-off), which is why it warrants a design doc.

## Goals / Non-Goals

**Goals:**
- Produce ~250 synthetic paraphrased goldens whose queries are answerable from — but not copied from — their single source chunk.
- Retain each golden's source `chunk_id` as the ground-truth retrieval target.
- Produce a ~50-golden raw-question baseline for a later synthetic-vs-real recall contrast.
- Use Claude (`claude-sonnet-5`) for generation + quality scoring, wired cleanly into DeepEval.
- Save reproducible datasets under `evals/quality/goldens/`.

**Non-Goals:**
- Running retrieval-quality metrics (CHO-19).
- Generator metrics (AnswerRelevancy, Faithfulness) — need an agent generator.
- Heavy evolutions, multi-hop / multi-context goldens.
- Any application/`src/` code; any writes to `kb_faq`.

## Decisions

**D1 — `generate_goldens_from_contexts`, not `from_docs`.**
The data is already chunked and curated in Postgres, so document loading, chunking (`chunk_size`/`chunk_overlap`), and context generation are all skipped. Chunks are pulled via `asyncpg` and passed as contexts.
*Alternative:* `generate_goldens_from_docs` — rejected; would re-chunk already-curated FAQs.

**D2 — One chunk = one context; keep a `context_index → kb_faq.id` map.**
Gives clean 1:1 ground truth: a golden generated from context *i* must retrieve `chunk_id = id_map[i]`. `max_goldens_per_context=1` → ~one fresh query per chunk.
*Alternative:* group chunks by section for multi-context goldens — rejected for v1 (multi-hop is a later, harder eval).

**D3 — Evolutions OFF for v1.**
DeepEval evolutions (IN_BREADTH, HYPOTHETICAL, COMPARATIVE, REASONING…) increase query complexity and, critically, can drift a query toward adjacent topics so it is no longer answerable from its single source chunk — which breaks the `chunk_id` ground-truth link and corrupts recall. Base generation already paraphrases away from the verbatim "Q:" wording, which is exactly the realism we need. Evolutions get turned up later, for a generation/answer-quality eval.

**D4 — Synthesize, don't reuse the real questions.**
Because the question text is inside the chunk, real questions yield circular retrieval (exact FTS + near-identical vector) and inflated recall. Synthetic paraphrases test generalization to real user phrasing. A small raw-question baseline is generated separately precisely to *measure* that inflation later (D7).

**D5 — Claude via a `DeepEvalBaseLLM` wrapper.**
DeepEval defaults to OpenAI; we wrap the Anthropic SDK (`claude-sonnet-5`) in a `DeepEvalBaseLLM` subclass (`generate` / `a_generate` / `get_model_name`) and pass it as the Synthesizer's `model`. If the installed `deepeval` ships a native Anthropic model class, prefer that. **The first implementation step verifies which path the installed version supports before any bulk generation.** `claude-sonnet-5` chosen for bulk gen+score (speed/cost) and domain fluency (DP/SLBM/NCD/CML jargon); `claude-opus-4-8` is the fallback if quality demands it.

**D6 — Topic-stratified sampling (~250).**
Sample ~250 distinct chunks proportionally across the 18 topics so all areas are represented; breadth of retrieval targets matters more than depth. Sampling is done in Python after `SELECT id, topic, chunk`.

**D7 — Raw-question baseline (~50), saved separately.**
A second, small dataset built directly from `kb_faq.question` (input = question, expected_output = answer, chunk_id = source id). Kept apart from the synthetic set so CHO-19 can report real-question recall (inflated) vs synthetic recall (honest) side by side.

**D8 — Structured, reproducible output.**
Both datasets saved as timestamped JSON under `evals/quality/goldens/`, each golden carrying `input`, `expected_output`, `context`, `chunk_id`, and available quality scores. `results/` gitignored.

## Risks / Trade-offs

- **`deepeval` API/model-wiring drift (not yet installed).** → First task pins the version, prints the Synthesizer signature, and confirms the wrapper works with one tiny `generate()` call before bulk generation.
- **Synthetic query leaks the verbatim question anyway.** → Dry-run eyeballs 5 goldens to confirm paraphrasing; if queries echo the "Q:" wording, adjust the generation prompt / instruction style.
- **Claude cost/time for ~300 gen+score calls.** → Trivial ($ few, minutes); no hard cap needed but the run logs counts.
- **Quality filter drops too many goldens** (context/input quality < 0.5) → final count may fall short of 250; log the drop and top up by sampling more chunks.
- **Domain jargon garbled by the model.** → `claude-sonnet-5` handles it; dry-run inspection catches nonsense before the full run.
- **Secrets in `.env`.** → Read-only DB; never log the DSN or keys; `.env` already git-ignored.

## Migration Plan

Additive only — new `evals/quality/` tree, `deepeval`/`anthropic` appended to `evals/requirements.txt`. No existing code or `kb_faq` modified. "Rollback" = delete the directory.

## Open Questions

_All resolved._ Sample size **~250** synthetic goldens; **~50** raw-question baseline set (saved separately); synthesizer model **`claude-sonnet-5`**; evolutions **off**. The only runtime unknown — native vs wrapper Anthropic integration — is resolved by the first implementation task against the installed `deepeval`.
