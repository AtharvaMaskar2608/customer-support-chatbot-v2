"""Golden-set generation for retrieval-quality evaluation (CHO-18).

Builds two datasets from the real ``kb_faq`` knowledge base, ready for the
downstream retrieval-quality eval (CHO-19):

  1. **Synthetic set (~250)** — DeepEval's ``Synthesizer`` (backed by Claude
     ``claude-sonnet-5``) paraphrases each sampled chunk into a fresh query via
     ``generate_goldens_from_contexts``. One chunk = one context (1:1 ground truth),
     ``max_goldens_per_context=1``, **evolutions OFF**. Each golden keeps its source
     ``chunk_id`` (``kb_faq.id``) as the retrieval target. Paraphrasing matters
     because ``kb_faq.chunk`` contains the verbatim ``Q:`` line — reusing it would
     make retrieval trivially circular.

  2. **Raw-question baseline (~50)** — built directly from real ``kb_faq.question``
     (input=question, expected_output=answer, chunk_id=id), saved separately so
     CHO-19 can contrast honest (synthetic) vs inflated (real-question) recall.

Both are topic-stratified across the 18 topics and saved as timestamped JSON under
``evals/quality/goldens/``. Read-only against ``kb_faq``.

Usage
-----
    python -m evals.quality.generate_goldens --dry-run    # 5 chunks, print, no save
    python -m evals.quality.generate_goldens              # full run (~250 + ~50)
    python -m evals.quality.generate_goldens --synthetic-n 250 --baseline-n 50

Requires ANTHROPIC_API_KEY and DATABASE_URL (+ PGPASSWORD) in the repo .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# DeepEval is noisy about telemetry / Confident-AI unless opted out; do it before import.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_UPDATE_WARNING_OPT_OUT", "YES")

try:  # module: python -m evals.quality.generate_goldens
    from .claude_model import DEFAULT_MODEL, make_claude_model, verify_model
except ImportError:  # script
    from claude_model import DEFAULT_MODEL, make_claude_model, verify_model  # type: ignore

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SYNTHETIC_TARGET = 250          # ~250 synthetic goldens, topic-stratified
BASELINE_TARGET = 50            # ~50 raw-question baseline goldens
QUALITY_THRESHOLD = 0.5         # DeepEval default synthetic_input_quality_threshold
MAX_CONCURRENT = 8              # in-flight Claude calls (default 100 trips timeouts)
SEED = 18                       # deterministic sampling (CHO-18)

GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"
REPO_ROOT = Path(__file__).resolve().parents[2]

SELECT_SQL = (
    "SELECT id, topic, question, answer, chunk "
    "FROM kb_faq WHERE embedding IS NOT NULL"
)


# --------------------------------------------------------------------------- #
# DB fetch (read-only)
# --------------------------------------------------------------------------- #
async def fetch_rows(dsn: str) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(SELECT_SQL)
    finally:
        await conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Topic-stratified sampling (proportional, ≥1 per topic, capped at availability)
# --------------------------------------------------------------------------- #
def stratified_sample(rows: list[dict], target: int, seed: int) -> list[dict]:
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_topic[r["topic"]].append(r)

    total = len(rows)
    topics = sorted(by_topic)  # stable order
    # Ideal proportional allocation, then largest-remainder rounding to hit `target`.
    ideal = {t: len(by_topic[t]) / total * target for t in topics}
    alloc = {t: min(len(by_topic[t]), max(1, math.floor(ideal[t]))) for t in topics}

    def total_alloc() -> int:
        return sum(alloc.values())

    # Adjust up/down to land on `target` (bounded by per-topic availability).
    remainders = sorted(topics, key=lambda t: ideal[t] - math.floor(ideal[t]), reverse=True)
    while total_alloc() < target:
        progressed = False
        for t in remainders:
            if alloc[t] < len(by_topic[t]):
                alloc[t] += 1
                progressed = True
                if total_alloc() >= target:
                    break
        if not progressed:  # every topic exhausted → sample is the whole table
            break
    while total_alloc() > target:
        for t in sorted(topics, key=lambda t: alloc[t], reverse=True):
            if alloc[t] > 1:
                alloc[t] -= 1
                if total_alloc() <= target:
                    break
        else:
            break

    rng = random.Random(seed)
    sample: list[dict] = []
    for t in topics:
        pool = by_topic[t][:]
        rng.shuffle(pool)
        sample.extend(pool[: alloc[t]])
    rng.shuffle(sample)
    return sample


# --------------------------------------------------------------------------- #
# Synthetic generation
# --------------------------------------------------------------------------- #
def build_contexts(sample: list[dict]):
    """contexts = [[chunk], ...]; maps for chunk→id and id→topic."""
    contexts = [[r["chunk"]] for r in sample]
    chunk_to_id: dict[str, int] = {r["chunk"]: r["id"] for r in sample}
    id_to_topic: dict[int, str] = {r["id"]: r["topic"] for r in sample}
    return contexts, chunk_to_id, id_to_topic


def _chunk_id_for(context, chunk_to_id: dict[str, int]):
    """Resolve a golden's context back to its source kb_faq.id (exact, then stripped)."""
    if not context:
        return None
    text = context[0] if isinstance(context, (list, tuple)) else context
    if text in chunk_to_id:
        return chunk_to_id[text]
    stripped = {k.strip(): v for k, v in chunk_to_id.items()}
    return stripped.get(str(text).strip())


def goldens_to_records(goldens, chunk_to_id, id_to_topic) -> list[dict]:
    records = []
    for g in goldens:
        chunk_id = _chunk_id_for(g.context, chunk_to_id)
        meta = g.additional_metadata or {}
        records.append({
            "input": g.input,
            "expected_output": g.expected_output,
            "context": list(g.context) if g.context else [],
            "chunk_id": chunk_id,
            "topic": id_to_topic.get(chunk_id),
            "synthetic_input_quality": meta.get("synthetic_input_quality"),
        })
    return records


# Styling steers the base generation toward realistic, reworded end-user phrasing.
# Without it, short FAQ questions ("What is a dividend?") come back verbatim — exactly
# the circular-retrieval trap this change exists to avoid (proposal / design R2).
STYLING = dict(
    scenario=(
        "Real Choice FinX brokerage customers asking a support chatbot about their "
        "demat/trading accounts, orders, charges, corporate actions, and platform "
        "features (FinX, StrikeX, SLBM, DP services)."
    ),
    task=(
        "Answer customer-support questions about the Choice FinX trading and demat "
        "platform, grounded in the knowledge base."
    ),
    input_format=(
        "A single natural-language question phrased the way a real customer would type "
        "it — conversational and self-contained. Reword the underlying question using "
        "DIFFERENT vocabulary and phrasing than the source FAQ; never reproduce the "
        "source question verbatim. Do not add meta preambles like 'in the context of'."
    ),
    expected_output_format="A concise, accurate answer grounded in the source context.",
)


def generate_synthetic(contexts, model, model_kind: str) -> list:
    from deepeval.synthesizer import Synthesizer
    from deepeval.synthesizer.config import EvolutionConfig, StylingConfig

    # Evolutions OFF (D3): base generation already paraphrases away from the verbatim
    # "Q:" wording; evolutions could drift a query off its single source chunk and
    # break the chunk_id ground-truth link. Filtration is left at its default
    # (threshold 0.5, critic = the same Claude model) by NOT passing a config.
    # cost_tracking stays OFF: the native AnthropicModel returns cost=None unless
    # per-token pricing is configured, and DeepEval does `synthesis_cost += cost`
    # unconditionally when tracking is on → TypeError. We don't need a dollar figure.
    # max_concurrent kept modest: the default 100 saturates the Anthropic endpoint
    # under a ~250-context batch and trips request timeouts, and one exhausted retry
    # aborts the whole gather. 8 in-flight is plenty and stays well within limits.
    synth = Synthesizer(
        model=model,
        max_concurrent=MAX_CONCURRENT,
        evolution_config=EvolutionConfig(num_evolutions=0),
        styling_config=StylingConfig(**STYLING),
        cost_tracking=False,
    )
    goldens = synth.generate_goldens_from_contexts(
        contexts=contexts,
        include_expected_output=True,
        max_goldens_per_context=1,
    )
    return goldens


# --------------------------------------------------------------------------- #
# Raw-question baseline
# --------------------------------------------------------------------------- #
def build_baseline(sample: list[dict]) -> list[dict]:
    return [{
        "input": r["question"],
        "expected_output": r["answer"],
        "context": [r["chunk"]],
        "chunk_id": r["id"],
        "topic": r["topic"],
        "synthetic_input_quality": None,  # real question, not synthesized
    } for r in sample]


# --------------------------------------------------------------------------- #
# Output + verification
# --------------------------------------------------------------------------- #
def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def save_dataset(records: list[dict], kind: str, stamp: str, meta: dict) -> Path:
    GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDENS_DIR / f"{kind}_goldens_{stamp}.json"
    payload = {"meta": meta, "goldens": records}
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path


def topic_coverage(records: list[dict]) -> Counter:
    return Counter(r["topic"] for r in records)


def print_coverage(title: str, records: list[dict], table_counts: Counter) -> None:
    cov = topic_coverage(records)
    n = len(records)
    print(f"\n  {title} — topic coverage ({n} goldens):")
    for t in sorted(table_counts, key=lambda t: table_counts[t], reverse=True):
        got = cov.get(t, 0)
        share = (got / n * 100) if n else 0
        table_share = table_counts[t] / sum(table_counts.values()) * 100
        print(f"    {got:>4} ({share:4.1f}%)  vs table {table_share:4.1f}%   {t}")


def sanity_check(records: list[dict], valid_ids: set[int], label: str) -> bool:
    ok = True
    missing = [r for r in records if r["chunk_id"] is None]
    invalid = [r for r in records if r["chunk_id"] is not None and r["chunk_id"] not in valid_ids]
    if missing:
        print(f"  ! {label}: {len(missing)} goldens have no chunk_id", file=sys.stderr)
        ok = False
    if invalid:
        print(f"  ! {label}: {len(invalid)} goldens have chunk_id not in kb_faq", file=sys.stderr)
        ok = False
    if ok:
        print(f"  ✓ {label}: all {len(records)} goldens carry a valid kb_faq chunk_id")
    return ok


def roundtrip_check(path: Path, expected_n: int, label: str) -> bool:
    with open(path) as f:
        loaded = json.load(f)
    got = len(loaded["goldens"])
    if got == expected_n:
        print(f"  ✓ {label}: round-trips ({got} goldens reloaded)")
        return True
    print(f"  ! {label}: round-trip mismatch (saved {expected_n}, loaded {got})", file=sys.stderr)
    return False


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(args) -> int:
    load_dotenv(REPO_ROOT / ".env")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set (repo .env).", file=sys.stderr)
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (repo .env).", file=sys.stderr)
        return 2

    # 1. Fetch corpus (read-only), then close the loop before DeepEval's own loop.
    rows = asyncio.run(fetch_rows(dsn))
    valid_ids = {r["id"] for r in rows}
    table_counts = Counter(r["topic"] for r in rows)
    print(f"kb_faq: {len(rows)} rows · {len(table_counts)} topics")

    # 2. Claude model (verified once before any bulk generation — task 2.3).
    model, kind = make_claude_model(DEFAULT_MODEL)
    print(f"model: {model.get_model_name()} [{kind}] — verify: {verify_model(model)!r}")

    stamp = timestamp()

    # 3. Synthetic set. Dry run uses a small flat random sample (stratified sampling
    # forces ≥1 per topic → a floor of 18); the full run is topic-stratified.
    if args.dry_run:
        syn_sample = random.Random(SEED).sample(rows, min(5, len(rows)))
    else:
        syn_sample = stratified_sample(rows, args.synthetic_n, SEED)
    contexts, chunk_to_id, id_to_topic = build_contexts(syn_sample)
    id_to_chunk = {r["id"]: r["chunk"] for r in syn_sample}
    print(f"\nSynthesizing from {len(contexts)} chunks "
          f"(evolutions OFF, max_goldens_per_context=1) ...")
    goldens = generate_synthetic(contexts, model, kind)
    syn_records = goldens_to_records(goldens, chunk_to_id, id_to_topic)
    dropped = len(contexts) - len(syn_records)
    print(f"  generated {len(syn_records)} goldens; "
          f"quality-filter/attrition dropped {dropped} of {len(contexts)} contexts")

    if args.dry_run:
        print("\n=== DRY RUN — eyeball paraphrase vs verbatim Q: ===")
        # Look the source chunk up by chunk_id — goldens come back in completion
        # order, so we can't align them to syn_sample positionally.
        for i, rec in enumerate(syn_records, 1):
            src_chunk = id_to_chunk.get(rec["chunk_id"], "")
            q_line = next((ln for ln in src_chunk.splitlines() if ln.startswith("Q:")), "")
            print(f"\n[{i}] chunk_id={rec['chunk_id']} topic={rec['topic']} "
                  f"quality={rec['synthetic_input_quality']}")
            print(f"    source  {q_line}")
            print(f"    query   {rec['input']!r}")
            print(f"    expected: {str(rec['expected_output'])[:160]!r}")
        print("\n(dry run — nothing saved)")
        return 0

    # 4. Raw-question baseline (separate dataset).
    base_sample = stratified_sample(rows, args.baseline_n, SEED + 1)
    base_records = build_baseline(base_sample)
    print(f"\nBaseline: {len(base_records)} raw-question goldens")

    # 5. Save both.
    syn_meta = {
        "kind": "synthetic", "generator": "deepeval.Synthesizer",
        "model": model.get_model_name(), "model_kind": kind,
        "evolutions": "off", "max_goldens_per_context": 1,
        "quality_threshold": QUALITY_THRESHOLD,
        "requested_contexts": len(contexts), "produced": len(syn_records),
        "dropped": dropped, "seed": SEED, "generated_at": stamp,
        "source_table": "kb_faq",
    }
    base_meta = {
        "kind": "baseline_raw_question", "model": None,
        "produced": len(base_records), "seed": SEED + 1,
        "generated_at": stamp, "source_table": "kb_faq",
        "note": "input is the verbatim kb_faq.question — retrieval is circular by design; "
                "used to quantify the synthetic-vs-real recall gap (CHO-19).",
    }
    syn_path = save_dataset(syn_records, "synthetic", stamp, syn_meta)
    base_path = save_dataset(base_records, "baseline", stamp, base_meta)

    # 6. Verify.
    print("\n== Sanity ==")
    ok = True
    ok &= sanity_check(syn_records, valid_ids, "synthetic")
    ok &= sanity_check(base_records, valid_ids, "baseline")
    ok &= roundtrip_check(syn_path, len(syn_records), "synthetic")
    ok &= roundtrip_check(base_path, len(base_records), "baseline")
    print_coverage("synthetic", syn_records, table_counts)

    print(f"\nSynthetic -> {syn_path}")
    print(f"Baseline  -> {base_path}")
    return 0 if ok else 1


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Generate the CHO-18 golden sets")
    ap.add_argument("--dry-run", action="store_true",
                    help="synthesize 5 chunks and print for eyeballing; save nothing")
    ap.add_argument("--synthetic-n", type=int, default=SYNTHETIC_TARGET,
                    help=f"synthetic goldens target (default {SYNTHETIC_TARGET})")
    ap.add_argument("--baseline-n", type=int, default=BASELINE_TARGET,
                    help=f"raw-question baseline target (default {BASELINE_TARGET})")
    return ap.parse_args(argv)


def main() -> int:
    return run(parse_args(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
