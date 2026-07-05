"""Retrieval-quality evaluation of the hybrid RRF pipeline (CHO-19).

Consumes the CHO-18 golden sets and answers the question CHO-16/CHO-17 could not:
does the CHO-17 hybrid retriever (dense vector + FTS + Reciprocal Rank Fusion over
``kb_faq``) actually surface the *right* chunk, and how well is it ranked?

For each golden:
  1. embed ``input`` (text-embedding-3-large @1536, matching kb_faq.embedding),
  2. run the CHO-17 ``SQL_RRF`` to get the ordered top-K chunk ids (reused verbatim),
  3. score two metric families:
       * deterministic ground-truth ``chunk_id`` metrics — recall / hit@k / MRR
         (cheap, reproducible, no LLM) → the headline number, and
       * DeepEval reference-based retriever metrics — ContextualRecall / Precision /
         Relevancy, Claude-judged (reusing the CHO-18 AnthropicModel wiring).

Headline deliverable: synthetic recall (honest) vs raw-question baseline recall
(circular/inflated), side by side — the gap CHO-18 was built to expose.

Usage
-----
    python -m evals.quality.eval_retrieval --dry-run             # 5 goldens/set, eyeball
    python -m evals.quality.eval_retrieval --metrics chunk_id    # zero-LLM, deterministic only
    python -m evals.quality.eval_retrieval                       # full run (all metrics)
    python -m evals.quality.eval_retrieval --min-quality 0.5 --limit 100

Read-only against kb_faq. Requires OPENAI_API_KEY, ANTHROPIC_API_KEY, DATABASE_URL in .env.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from openai import AsyncOpenAI

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_UPDATE_WARNING_OPT_OUT", "YES")

# Reuse the CHO-17 retrieval path verbatim (same SQL, same embedding recipe, same config)
# so we measure the real pipeline, not a reimplementation.
from evals.retrieval import config as rconfig
from evals.retrieval.benchmark_retrieval import SQL_RRF, embed_texts

try:  # module: python -m evals.quality.eval_retrieval
    from .claude_model import DEFAULT_MODEL, make_claude_model
except ImportError:  # script
    from claude_model import DEFAULT_MODEL, make_claude_model  # type: ignore

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
REPO_ROOT = Path(__file__).resolve().parents[2]

HIT_KS = (1, 3, 5, 10)
MAX_CONCURRENT = 8               # LLM-metric concurrency (CHO-18: default 100 → timeouts)
POOL_MAX = 12

# metric key -> DeepEval metric class name (lazy import to keep --metrics chunk_id LLM-free)
LLM_METRICS = ("contextual_recall", "contextual_precision", "contextual_relevancy")


# --------------------------------------------------------------------------- #
# Golden loading
# --------------------------------------------------------------------------- #
def latest(kind: str) -> Path | None:
    hits = sorted(glob.glob(str(GOLDENS_DIR / f"{kind}_goldens_*.json")))
    return Path(hits[-1]) if hits else None


def load_goldens(kind: str, limit: int | None, min_quality: float) -> tuple[list[dict], int]:
    """Load a golden set; filter synthetic by quality. Returns (goldens, excluded_count)."""
    path = latest(kind)
    if path is None:
        return [], 0
    data = json.load(open(path))["goldens"]
    excluded = 0
    if min_quality > 0:
        kept = []
        for g in data:
            q = g.get("synthetic_input_quality")
            if q is not None and q < min_quality:
                excluded += 1
            else:
                kept.append(g)
        data = kept
    if limit is not None:
        data = data[:limit]
    return data, excluded


# --------------------------------------------------------------------------- #
# Retrieval (reused CHO-17 path)
# --------------------------------------------------------------------------- #
async def retrieve(pool, vec_literal: str, query: str) -> list[int]:
    """Ordered top-K kb_faq ids from the RRF query (rank order preserved)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(SQL_RRF, vec_literal, query,
                                rconfig.N_CANDIDATES, rconfig.TOP_K)
    return [r["id"] for r in rows]


async def fetch_chunks(pool, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, chunk FROM kb_faq WHERE id = ANY($1::bigint[])", ids)
    return {r["id"]: r["chunk"] for r in rows}


# --------------------------------------------------------------------------- #
# Deterministic chunk-id metrics
# --------------------------------------------------------------------------- #
def chunk_id_metrics(retrieved_ids: list[int], gt_id: int) -> dict:
    rank = retrieved_ids.index(gt_id) + 1 if gt_id in retrieved_ids else None
    m = {"rank": rank, "recall": rank is not None, "mrr": (1.0 / rank) if rank else 0.0}
    for k in HIT_KS:
        m[f"hit@{k}"] = rank is not None and rank <= k
    return m


# --------------------------------------------------------------------------- #
# DeepEval LLM metrics
# --------------------------------------------------------------------------- #
def build_metric(key: str, model):
    from deepeval.metrics import (ContextualRecallMetric, ContextualPrecisionMetric,
                                  ContextualRelevancyMetric)
    cls = {"contextual_recall": ContextualRecallMetric,
           "contextual_precision": ContextualPrecisionMetric,
           "contextual_relevancy": ContextualRelevancyMetric}[key]
    return cls(model=model, async_mode=True, include_reason=False)


async def score_llm_metrics(golden: dict, retrieval_context: list[str],
                            metric_keys: tuple[str, ...], model) -> dict:
    """Score requested DeepEval metrics for one golden; failures record None."""
    from deepeval.test_case import LLMTestCase
    tc = LLMTestCase(
        input=golden["input"],
        actual_output="",  # unused by retriever metrics; kept empty
        expected_output=golden["expected_output"],
        retrieval_context=retrieval_context,
    )
    out: dict = {}
    for key in metric_keys:
        try:
            m = build_metric(key, model)
            await m.a_measure(tc, _show_indicator=False)
            out[key] = m.score
        except Exception as e:  # noqa: BLE001 — one metric failure shouldn't abort the run
            out[key] = None
            out[f"{key}__error"] = type(e).__name__
    return out


# --------------------------------------------------------------------------- #
# Evaluate one dataset
# --------------------------------------------------------------------------- #
async def evaluate(dataset: str, goldens: list[dict], pool, client, model,
                   metric_keys: tuple[str, ...]) -> list[dict]:
    if not goldens:
        return []
    # Embed all inputs (one batched call), reuse the CHO-17 helper → pgvector literals.
    vecs = await embed_texts(client, [g["input"] for g in goldens])

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def one(i: int, g: dict) -> dict:
        async with sem:
            ids = await retrieve(pool, vecs[i], g["input"])
            id2chunk = await fetch_chunks(pool, ids)
            retrieval_context = [id2chunk[i_] for i_ in ids if i_ in id2chunk]
            rec = {
                "dataset": dataset,
                "input": g["input"],
                "chunk_id": g["chunk_id"],
                "topic": g.get("topic"),
                "synthetic_input_quality": g.get("synthetic_input_quality"),
                "retrieved_ids": ids,
                **chunk_id_metrics(ids, g["chunk_id"]),
            }
            if metric_keys:
                rec.update(await score_llm_metrics(g, retrieval_context, metric_keys, model))
            return rec

    return list(await asyncio.gather(*(one(i, g) for i, g in enumerate(goldens))))


# --------------------------------------------------------------------------- #
# Aggregation + output
# --------------------------------------------------------------------------- #
def _mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def aggregate(dataset: str, records: list[dict], metric_keys: tuple[str, ...]) -> dict:
    n = len(records)
    agg = {
        "dataset": dataset,
        "n": n,
        "recall": _mean([r["recall"] for r in records]),
        "mrr": _mean([r["mrr"] for r in records]),
    }
    for k in HIT_KS:
        agg[f"hit@{k}"] = _mean([r[f"hit@{k}"] for r in records])
    for key in metric_keys:
        scored = [r.get(key) for r in records if r.get(key) is not None]
        agg[key] = statistics.mean(scored) if scored else None
        agg[f"{key}__scored"] = len(scored)  # coverage: how many goldens the judge scored
    return agg


def per_topic(records: list[dict]) -> dict[str, dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by[r["topic"]].append(r)
    return {t: {"n": len(rs), "recall": _mean([r["recall"] for r in rs]),
                "hit@1": _mean([r["hit@1"] for r in rs]),
                "hit@3": _mean([r["hit@3"] for r in rs])}
            for t, rs in by.items()}


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _pct(v):
    return "-" if v is None else f"{v*100:5.1f}%"


def print_report(aggs: dict[str, dict], topics: dict[str, dict], metric_keys, excluded, meta):
    print("\n== Retrieval quality (deterministic chunk_id ground truth) ==")
    hdr = f"{'dataset':<11}{'n':>5}{'recall':>9}{'hit@1':>8}{'hit@3':>8}{'hit@5':>8}{'hit@10':>8}{'MRR':>8}"
    print(hdr); print("-" * len(hdr))
    for ds, a in aggs.items():
        mrr = "-" if a["mrr"] is None else f"{a['mrr']:.3f}"
        print(f"{ds:<11}{a['n']:>5}{_pct(a['recall']):>9}{_pct(a['hit@1']):>8}"
              f"{_pct(a['hit@3']):>8}{_pct(a['hit@5']):>8}{_pct(a['hit@10']):>8}{mrr:>8}")

    if "synthetic" in aggs and "baseline" in aggs:
        s, b = aggs["synthetic"]["recall"], aggs["baseline"]["recall"]
        if s is not None and b is not None:
            print(f"\n  Circularity gap (baseline − synthetic recall): "
                  f"{(b - s)*100:+.1f} pp  (baseline {_pct(b).strip()}, synthetic {_pct(s).strip()})")

    if metric_keys:
        print("\n== DeepEval reference-based metrics (Claude-judged, mean over scored) ==")
        mh = f"{'dataset':<11}" + "".join(f"{k.replace('contextual_',''):>14}" for k in metric_keys)
        print(mh); print("-" * len(mh))
        for ds, a in aggs.items():
            row = f"{ds:<11}" + "".join(
                ("-" if a.get(k) is None else f"{a[k]:.3f}").rjust(14) for k in metric_keys)
            print(row)
        # coverage — relevancy occasionally errors on the judge side (unparseable output)
        cov = {a["dataset"]: {k: a.get(f"{k}__scored") for k in metric_keys} for a in aggs.values()}
        for ds, c in cov.items():
            gaps = [f"{k.replace('contextual_','')} {v}/{aggs[ds]['n']}"
                    for k, v in c.items() if v is not None and v < aggs[ds]["n"]]
            if gaps:
                print(f"    ({ds}: judge scored {', '.join(gaps)} — rest errored, excluded from mean)")

    print("\n== Synthetic recall by topic ==")
    st = topics.get("synthetic", {})
    for t in sorted(st, key=lambda t: st[t]["n"], reverse=True):
        d = st[t]
        print(f"    {d['n']:>4}  recall {_pct(d['recall'])}  hit@1 {_pct(d['hit@1'])}   {t}")
    if excluded:
        print(f"\n  ({excluded} synthetic goldens excluded below --min-quality)")


def save_results(records: list[dict], aggs, topics, metric_keys, meta, stamp) -> tuple[Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    jpath = RESULTS_DIR / f"retrieval_quality_{stamp}.json"
    json.dump({"meta": meta, "aggregate": aggs, "per_topic": topics, "records": records},
              open(jpath, "w"), indent=2, ensure_ascii=False)

    cpath = RESULTS_DIR / f"retrieval_quality_{stamp}.csv"
    cols = (["dataset", "n", "recall", "mrr"] + [f"hit@{k}" for k in HIT_KS]
            + [c for key in metric_keys for c in (key, f"{key}__scored")])
    with open(cpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for a in aggs.values():  # each aggregate carries its own "dataset" name
            w.writerow({c: a.get(c) for c in cols})
    return jpath, cpath


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def resolve_metrics(arg: str) -> tuple[str, ...]:
    arg = arg.strip().lower()
    if arg in ("chunk_id", "none", ""):
        return ()
    if arg == "all":
        return LLM_METRICS
    picked = []
    for tok in arg.split(","):
        tok = tok.strip()
        key = tok if tok.startswith("contextual_") else f"contextual_{tok}"
        if key in LLM_METRICS:
            picked.append(key)
        else:
            raise SystemExit(f"unknown metric {tok!r}; choose from recall,precision,relevancy,chunk_id,all")
    return tuple(picked)


async def main_async(args) -> int:
    load_dotenv(REPO_ROOT / ".env")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set (repo .env).", file=sys.stderr)
        return 2
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set (repo .env).", file=sys.stderr)
        return 2

    metric_keys = resolve_metrics(args.metrics)
    if metric_keys and not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set but LLM metrics requested.", file=sys.stderr)
        return 2

    limit = 5 if args.dry_run else args.limit
    which = ["synthetic", "baseline"] if args.dataset == "both" else [args.dataset]
    datasets = {}
    total_excluded = 0
    for kind in which:
        gs, exc = load_goldens(kind, limit, args.min_quality if kind == "synthetic" else 0.0)
        datasets[kind] = gs
        total_excluded += exc
        if not gs:
            print(f"  ! no goldens found for '{kind}' in {GOLDENS_DIR}", file=sys.stderr)

    if not any(datasets.values()):
        print("No goldens to evaluate.", file=sys.stderr)
        return 1

    client = AsyncOpenAI(timeout=rconfig.TIMEOUT_S)
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=POOL_MAX)
    model = None
    if metric_keys:
        model, kind = make_claude_model(DEFAULT_MODEL)
        print(f"metric model: {model.get_model_name()} [{kind}]")

    try:
        dims = await pool.fetchval(
            "SELECT vector_dims(embedding) FROM kb_faq WHERE embedding IS NOT NULL LIMIT 1")
        assert dims == rconfig.EMBED_DIMS, f"kb_faq dims {dims} != expected {rconfig.EMBED_DIMS}"
        print(f"kb_faq embedding dims OK ({dims}); TOP_K={rconfig.TOP_K} "
              f"N_CANDIDATES={rconfig.N_CANDIDATES} RRF_K={rconfig.RRF_K}")
        print(f"metrics: {', '.join(k.replace('contextual_','') for k in metric_keys) or 'chunk_id only (no LLM)'}")

        all_records: list[dict] = []
        aggs: dict[str, dict] = {}
        topics: dict[str, dict] = {}
        for kind in which:
            gs = datasets[kind]
            if not gs:
                continue
            print(f"\nEvaluating {kind}: {len(gs)} goldens ...", flush=True)
            recs = await evaluate(kind, gs, pool, client, model, metric_keys)
            all_records.extend(recs)
            aggs[kind] = aggregate(kind, recs, metric_keys)
            topics[kind] = per_topic(recs)
    finally:
        await pool.close()
        await client.close()

    if args.dry_run:
        print("\n=== DRY RUN — sample retrievals ===")
        for r in all_records[:5]:
            hit = "✓" if r["recall"] else "✗"
            print(f"\n  [{hit}] {r['dataset']} chunk_id={r['chunk_id']} rank={r['rank']} topic={r['topic']}")
            print(f"      query:     {r['input']!r}")
            print(f"      retrieved: {r['retrieved_ids']}")
            if metric_keys:
                print("      metrics:   " + ", ".join(
                    f"{k.replace('contextual_','')}={r.get(k)}" for k in metric_keys))

    stamp = timestamp()
    meta = {"generated_at": stamp, "top_k": rconfig.TOP_K, "n_candidates": rconfig.N_CANDIDATES,
            "rrf_k": rconfig.RRF_K, "embed_model": rconfig.EMBED_MODEL, "embed_dims": rconfig.EMBED_DIMS,
            "metrics": list(metric_keys), "min_quality": args.min_quality,
            "excluded_below_min_quality": total_excluded, "dry_run": args.dry_run}
    print_report(aggs, topics, metric_keys, total_excluded, meta)

    if not args.dry_run:
        jpath, cpath = save_results(all_records, aggs, topics, metric_keys, meta, stamp)
        print(f"\nJSON -> {jpath}")
        print(f"CSV  -> {cpath}")
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Retrieval-quality eval (CHO-19)")
    ap.add_argument("--dry-run", action="store_true", help="5 goldens/set, print samples, no save")
    ap.add_argument("--limit", type=int, default=None, help="max goldens per dataset")
    ap.add_argument("--metrics", default="all",
                    help="'all' | 'chunk_id' (no LLM) | comma list of recall,precision,relevancy")
    ap.add_argument("--min-quality", type=float, default=0.0,
                    help="drop synthetic goldens below this synthetic_input_quality")
    ap.add_argument("--dataset", choices=["both", "synthetic", "baseline"], default="both")
    return ap.parse_args(argv)


def main() -> int:
    return asyncio.run(main_async(parse_args(sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
