"""Hybrid-retrieval latency benchmark (CHO-17).

Measures per-stage latency of RRF-only hybrid retrieval — dense vector + full-text
+ Reciprocal Rank Fusion, all in Postgres — over the real ``kb_faq`` table.

Three passes:
  A. pure retrieval   — embed the query set ONCE, reuse vectors, time the SQL path
                        (vector-only / FTS-only / RRF-combined) → isolates infra latency
  B. end-to-end       — live embed + RRF together → shows embed's share of wall-clock
  C. concurrency      — RRF at concurrency 1 / 10 / 25 via the asyncpg pool

Also records EXPLAIN (ANALYZE) for the vector and RRF queries so the timed path is
documented, not assumed. Read-only against kb_faq.

Usage
-----
    python -m evals.retrieval.benchmark_retrieval             # full run
    python -m evals.retrieval.benchmark_retrieval --dry-run   # quick wiring check

Requires DATABASE_URL and OPENAI_API_KEY in the repo .env.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv
from openai import AsyncOpenAI

try:  # module: python -m evals.retrieval.benchmark_retrieval
    from . import config
except ImportError:  # script
    import config  # type: ignore


# --------------------------------------------------------------------------- #
# SQL — the two arms and the single-round-trip RRF fusion
# --------------------------------------------------------------------------- #
SQL_VECTOR = """
SELECT id FROM kb_faq
ORDER BY embedding <=> $1::vector({dims}) LIMIT $2
""".format(dims=config.EMBED_DIMS)

SQL_FTS = """
SELECT id
FROM kb_faq, plainto_tsquery('english', $1) q
WHERE tsv @@ q
ORDER BY ts_rank_cd(tsv, q) DESC LIMIT $2
"""

# RRF: rank each arm, FULL OUTER JOIN, sum 1/(k+rank). COALESCE handles a doc that
# appears in only one arm. $1 vector literal, $2 query text, $3 candidate depth N, $4 top-K.
SQL_RRF = """
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> $1::vector({dims})) AS rank
  FROM kb_faq ORDER BY embedding <=> $1::vector({dims}) LIMIT $3),
fts AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rank
  FROM kb_faq, plainto_tsquery('english', $2) q
  WHERE tsv @@ q ORDER BY ts_rank_cd(tsv, q) DESC LIMIT $3)
SELECT id,
       COALESCE(1.0/({k} + vec.rank), 0) + COALESCE(1.0/({k} + fts.rank), 0) AS rrf
FROM vec FULL OUTER JOIN fts USING (id)
ORDER BY rrf DESC LIMIT $4
""".format(dims=config.EMBED_DIMS, k=config.RRF_K)


# --------------------------------------------------------------------------- #
# Result record + metrics helpers
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    pass_name: str      # "A-pure" | "B-e2e" | "C-concurrency"
    arm: str            # "vector" | "fts" | "rrf" | "embed+rrf"
    concurrency: int
    trials: int
    ok: int
    p50: float | None
    p95: float | None
    p99: float | None
    mean: float | None
    max: float | None
    throughput_per_s: float


def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarize(pass_name: str, arm: str, concurrency: int, latencies: list[float],
              wall_s: float, trials: int) -> Row:
    lat = sorted(latencies)
    return Row(
        pass_name=pass_name, arm=arm, concurrency=concurrency, trials=trials,
        ok=len(lat),
        p50=percentile(lat, 0.50), p95=percentile(lat, 0.95), p99=percentile(lat, 0.99),
        mean=statistics.mean(lat) if lat else None, max=max(lat) if lat else None,
        throughput_per_s=(len(lat) / wall_s) if wall_s > 0 else 0.0,
    )


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


# --------------------------------------------------------------------------- #
# Embedding
# --------------------------------------------------------------------------- #
async def embed_texts(client: AsyncOpenAI, texts: list[str]) -> list[str]:
    """Embed with large@1536; assert dim; return pgvector literals."""
    resp = await client.embeddings.create(
        model=config.EMBED_MODEL, input=texts, dimensions=config.EMBED_DIMS)
    out = []
    for d in resp.data:
        assert len(d.embedding) == config.EMBED_DIMS, \
            f"expected {config.EMBED_DIMS} dims, got {len(d.embedding)}"
        out.append(to_pgvector(d.embedding))
    return out


# --------------------------------------------------------------------------- #
# Timing core
# --------------------------------------------------------------------------- #
async def timed_fetch(pool: asyncpg.Pool, sql: str, params: tuple) -> float:
    """Acquire a pooled connection, run the query, return latency in ms."""
    t0 = time.perf_counter()
    async with pool.acquire() as conn:
        await conn.fetch(sql, *params)
    return (time.perf_counter() - t0) * 1000.0


async def run_arm(pool, pass_name, arm, sql, param_list, trials, warmup, concurrency):
    """Run `trials` timed calls of one arm, cycling params, at the given concurrency."""
    n = len(param_list)

    async def call(i):
        return await timed_fetch(pool, sql, param_list[i % n])

    # warmup (discarded)
    if warmup:
        await asyncio.gather(*(call(i) for i in range(warmup)))

    latencies: list[float] = []
    t_start = time.perf_counter()
    if concurrency <= 1:
        for i in range(trials):
            latencies.append(await call(i))
    else:
        sem = asyncio.Semaphore(concurrency)

        async def guarded(i):
            async with sem:
                return await call(i)
        latencies = list(await asyncio.gather(*(guarded(i) for i in range(trials))))
    wall_s = time.perf_counter() - t_start
    return summarize(pass_name, arm, concurrency, latencies, wall_s, trials)


# --------------------------------------------------------------------------- #
# Passes
# --------------------------------------------------------------------------- #
async def sample_queries(pool, k: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT question FROM kb_faq WHERE question IS NOT NULL "
        "ORDER BY random() LIMIT $1", k)
    return [r["question"] for r in rows]


async def capture_plans(pool, vec_literal: str, qtext: str) -> str:
    """EXPLAIN (ANALYZE, FORMAT TEXT) for the vector and RRF queries."""
    out = []
    async with pool.acquire() as conn:
        for label, sql, params in [
            ("VECTOR arm", SQL_VECTOR, (vec_literal, config.N_CANDIDATES)),
            ("RRF", SQL_RRF, (vec_literal, qtext, config.N_CANDIDATES, config.TOP_K)),
        ]:
            rows = await conn.fetch("EXPLAIN (ANALYZE, FORMAT TEXT) " + sql, *params)
            out.append(f"===== {label} =====")
            out.extend(r["QUERY PLAN"] for r in rows)
            out.append("")
    return "\n".join(out)


async def run_all(pool, client, trials, warmup, dry_run):
    results: list[Row] = []

    # Query set + one-time embedding (Pass A / C reuse these vectors).
    queries = await sample_queries(pool, config.QUERY_SET_SIZE if not dry_run else 5)
    q_lens = [len(q) for q in queries]
    print(f"  query set: {len(queries)} sampled from kb_faq.question "
          f"(avg {statistics.mean(q_lens):.0f} chars)")
    vecs = await embed_texts(client, queries)  # embedded ONCE
    vec_params = [(vecs[i], config.N_CANDIDATES) for i in range(len(queries))]
    fts_params = [(queries[i], config.N_CANDIDATES) for i in range(len(queries))]
    rrf_params = [(vecs[i], queries[i], config.N_CANDIDATES, config.TOP_K)
                  for i in range(len(queries))]

    # Record the query plan (documents seq-scan vs index-scan reality).
    plan_text = await capture_plans(pool, vecs[0], queries[0])
    print("  captured EXPLAIN ANALYZE plans")

    # ---- Pass A: pure retrieval (cached vectors, concurrency 1) ----
    print("  Pass A — pure retrieval (vector / fts / rrf) ...", flush=True)
    results.append(await run_arm(pool, "A-pure", "vector", SQL_VECTOR, vec_params, trials, warmup, 1))
    results.append(await run_arm(pool, "A-pure", "fts", SQL_FTS, fts_params, trials, warmup, 1))
    results.append(await run_arm(pool, "A-pure", "rrf", SQL_RRF, rrf_params, trials, warmup, 1))

    # ---- Pass B: end-to-end (live embed + rrf) ----
    print("  Pass B — end-to-end (live embed + rrf) ...", flush=True)
    b_lat: list[float] = []
    b_trials = min(trials, 30) if not dry_run else 3  # live embeds are the slow part
    for i in range(b_trials):
        q = f"{queries[i % len(queries)]} [n={i}]"     # nonce → uncached embed
        t0 = time.perf_counter()
        lit = (await embed_texts(client, [q]))[0]
        async with pool.acquire() as conn:
            await conn.fetch(SQL_RRF, lit, q, config.N_CANDIDATES, config.TOP_K)
        b_lat.append((time.perf_counter() - t0) * 1000.0)
    results.append(summarize("B-e2e", "embed+rrf", 1, b_lat,
                             sum(b_lat) / 1000.0 if b_lat else 0.0, b_trials))

    # ---- Pass C: concurrency (rrf, cached vectors) ----
    if not dry_run:
        for c in config.CONCURRENCY:
            print(f"  Pass C — rrf @ concurrency {c} ...", flush=True)
            results.append(await run_arm(pool, "C-concurrency", "rrf", SQL_RRF,
                                         rrf_params, trials, warmup, c))

    return results, plan_text, len(queries)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fmt(v, nd=1):
    return "-" if v is None else f"{v:.{nd}f}"


def print_table(results: list[Row]) -> None:
    header = f"{'pass':<15}{'arm':<12}{'conc':>6}{'ok':>5}{'p50':>9}{'p95':>9}{'p99':>9}{'q/s':>9}"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        print(f"{r.pass_name:<15}{r.arm:<12}{r.concurrency:>6}{r.ok:>5}"
              f"{_fmt(r.p50):>9}{_fmt(r.p95):>9}{_fmt(r.p99):>9}{r.throughput_per_s:>9.1f}")
    print()


def write_csv(results, stamp):
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RESULTS_DIR / f"retrieval_latency_{stamp}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))
    return path


def write_plan(plan_text, count, stamp):
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RESULTS_DIR / f"retrieval_plan_{stamp}.txt"
    with open(path, "w") as f:
        f.write(f"kb_faq row count: {count}\n\n{plan_text}\n")
    return path


def write_plot(results, stamp):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # noqa: BLE001
        print(f"  ! plot skipped ({e})")
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    # Panel 1: stage breakdown (p50) — pass A arms + pass B end-to-end
    stage = {r.arm: r.p50 for r in results if r.pass_name == "A-pure"}
    b = next((r for r in results if r.pass_name == "B-e2e"), None)
    if b:
        stage["embed+rrf\n(e2e)"] = b.p50
    labels = list(stage.keys())
    ax1.bar(labels, [stage[k] or 0 for k in labels])
    ax1.set_title("Stage latency (p50, ms)")
    ax1.set_ylabel("ms")
    ax1.grid(True, axis="y", alpha=0.3)

    # Panel 2: rrf latency vs concurrency (pass C)
    cpts = sorted([r for r in results if r.pass_name == "C-concurrency"],
                  key=lambda r: r.concurrency)
    if cpts:
        xs = [r.concurrency for r in cpts]
        for metric, lbl in [("p50", "p50"), ("p95", "p95"), ("p99", "p99")]:
            ax2.plot(xs, [getattr(r, metric) for r in cpts], marker="o", label=lbl)
        ax2.set_title("RRF latency vs concurrency")
        ax2.set_xlabel("concurrency")
        ax2.set_ylabel("ms")
        ax2.legend()
        ax2.grid(True, alpha=0.3)
    fig.tight_layout()
    path = config.RESULTS_DIR / f"retrieval_latency_{stamp}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def main_async(args) -> int:
    load_dotenv(config.REPO_ROOT / ".env")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set (repo .env).", file=sys.stderr)
        return 2
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set (repo .env).", file=sys.stderr)
        return 2

    client = AsyncOpenAI(timeout=config.TIMEOUT_S)
    pool = await asyncpg.create_pool(dsn, min_size=config.POOL_MIN, max_size=config.POOL_MAX)
    try:
        # Schema assertions + corpus size.
        udt = await pool.fetchval(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name='kb_faq' AND column_name='embedding'")
        count = await pool.fetchval("SELECT count(*) FROM kb_faq")
        dims = await pool.fetchval(
            "SELECT vector_dims(embedding) FROM kb_faq WHERE embedding IS NOT NULL LIMIT 1")
        print(f"kb_faq: {count} rows · embedding {udt}({dims})")
        assert dims == config.EMBED_DIMS, f"column dims {dims} != expected {config.EMBED_DIMS}"

        trials = min(config.TRIALS, 5) if args.dry_run else config.TRIALS
        warmup = min(config.WARMUP, 2) if args.dry_run else config.WARMUP
        results, plan_text, qn = await run_all(pool, client, trials, warmup, args.dry_run)
    finally:
        await pool.close()
        await client.close()

    if not results:
        print("No results.", file=sys.stderr)
        return 1

    stamp = timestamp()
    print_table(results)
    print(f"CSV  -> {write_csv(results, stamp)}")
    print(f"Plan -> {write_plan(plan_text, count, stamp)}")
    if not args.dry_run:
        p = write_plot(results, stamp)
        if p:
            print(f"Plot -> {p}")
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Hybrid-retrieval latency benchmark")
    ap.add_argument("--dry-run", action="store_true",
                    help="few queries/trials, no plot — wiring check")
    return ap.parse_args(argv)


def main() -> int:
    return asyncio.run(main_async(parse_args(sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
