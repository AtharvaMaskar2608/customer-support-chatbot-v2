"""Query-embedding latency & cost benchmark (CHO-16).

Measures OpenAI ``text-embedding-3-small`` vs ``text-embedding-3-large`` at query
time across a ``model x dims x concurrency`` grid, reporting RAW, UNCACHED numbers.

Caching is controlled for so the measurement reflects the real model:
  * every call embeds unique text (per-call nonce) -> no response cache can
    deflate p95;
  * warmup calls run first and their timings are discarded;
  * HTTP keep-alive stays on (connection warmth, not a warm cache).

Cost is computed locally from tiktoken token counts x per-model list price.

Usage
-----
    # Full grid (real, billed API calls):
    python -m evals.embeddings.benchmark_embeddings

    # Cheap wiring check (1 model, concurrency 1, few trials, no plot):
    python -m evals.embeddings.benchmark_embeddings --dry-run

Requires ``OPENAI_API_KEY`` (loaded from the repo ``.env``).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from dotenv import load_dotenv
import tiktoken
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

try:  # run as a module: python -m evals.embeddings.benchmark_embeddings
    from . import config
    from .queries import QUERIES
except ImportError:  # run as a script: python evals/embeddings/benchmark_embeddings.py
    import config  # type: ignore
    from queries import QUERIES  # type: ignore


# --------------------------------------------------------------------------- #
# Result record
# --------------------------------------------------------------------------- #
@dataclass
class ConfigResult:
    """One row of results for a single (model, dims, concurrency) configuration."""
    model: str
    dims: int
    concurrency: int
    trials: int
    ok: int
    # latency (ms)
    p50: float | None
    p95: float | None
    p99: float | None
    mean: float | None
    max: float | None
    # throughput / reliability
    throughput_per_s: float
    error_rate: float
    timeout_rate: float
    rate_limit_rate: float
    n_429: int
    # cost
    avg_query_tokens: float
    cost_per_query: float
    cost_per_1m_queries: float


# --------------------------------------------------------------------------- #
# Metrics helpers
# --------------------------------------------------------------------------- #
def percentile(sorted_vals: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in [0, 1]) over an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #
_enc = tiktoken.get_encoding(config.TOKEN_ENCODING)


def avg_query_tokens() -> float:
    """Average token count of the (nonce-free) query pool, for cost estimation."""
    return statistics.mean(len(_enc.encode(q)) for q in QUERIES)


def make_input(i: int) -> str:
    """A query seed plus a per-call nonce so no two calls embed identical text."""
    base = QUERIES[i % len(QUERIES)]
    return f"{base} [n={i}-{random.randint(0, 1_000_000)}]"


# --------------------------------------------------------------------------- #
# Single call (with bounded retry + jitter; raw 429s still counted)
# --------------------------------------------------------------------------- #
async def timed_call(client: AsyncOpenAI, model: str, dims: int, text: str):
    """Time one embedding call.

    Returns ``(latency_ms | None, kind, n_429)`` where kind is one of
    ``ok | timeout | rate_limit | error``. ``n_429`` counts every 429 seen,
    including ones that were retried and later succeeded.
    """
    n_429 = 0
    for attempt in range(config.MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            await client.embeddings.create(model=model, input=text, dimensions=dims)
            return (time.perf_counter() - t0) * 1000.0, "ok", n_429
        except RateLimitError:
            n_429 += 1
            kind = "rate_limit"
        except APITimeoutError:
            kind = "timeout"
        except APIError:
            kind = "error"
        if attempt == config.MAX_RETRIES:
            return None, kind, n_429
        await asyncio.sleep(
            config.RETRY_BASE_S * (2 ** attempt) + random.uniform(0, config.RETRY_BASE_S)
        )
    return None, "error", n_429


# --------------------------------------------------------------------------- #
# One configuration
# --------------------------------------------------------------------------- #
async def run_config(
    client: AsyncOpenAI,
    model: str,
    dims: int,
    concurrency: int,
    trials: int,
    warmup: int,
    avg_tokens: float,
    price_per_1m: float,
) -> ConfigResult:
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int):
        async with sem:
            return await timed_call(client, model, dims, make_input(i))

    # Warmup: run and discard timings (cold TLS/connection setup must not count).
    if warmup:
        await asyncio.gather(*(one(i) for i in range(warmup)))

    t_start = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(trials)))
    wall_s = time.perf_counter() - t_start

    lat = sorted(r[0] for r in results if r[1] == "ok" and r[0] is not None)
    n_ok = len(lat)
    n_timeout = sum(1 for r in results if r[1] == "timeout")
    n_error = sum(1 for r in results if r[1] == "error")
    n_rate = sum(1 for r in results if r[1] == "rate_limit")
    n_429 = sum(r[2] for r in results)

    cost_per_query = avg_tokens / 1_000_000 * price_per_1m
    n = max(trials, 1)

    return ConfigResult(
        model=model,
        dims=dims,
        concurrency=concurrency,
        trials=trials,
        ok=n_ok,
        p50=percentile(lat, 0.50),
        p95=percentile(lat, 0.95),
        p99=percentile(lat, 0.99),
        mean=statistics.mean(lat) if lat else None,
        max=max(lat) if lat else None,
        throughput_per_s=(n_ok / wall_s) if wall_s > 0 else 0.0,
        error_rate=n_error / n,
        timeout_rate=n_timeout / n,
        rate_limit_rate=n_rate / n,
        n_429=n_429,
        avg_query_tokens=avg_tokens,
        cost_per_query=cost_per_query,
        cost_per_1m_queries=cost_per_query * 1_000_000,
    )


# --------------------------------------------------------------------------- #
# Grid
# --------------------------------------------------------------------------- #
def build_grid(models: list[str], concurrency: list[int]) -> list[tuple[str, int, int]]:
    grid: list[tuple[str, int, int]] = []
    for model in models:
        for dims in config.MODELS[model]["dims"]:
            for c in concurrency:
                grid.append((model, dims, c))
    return grid


async def run_grid(client, grid, trials, warmup) -> list[ConfigResult]:
    avg_tokens = avg_query_tokens()
    results: list[ConfigResult] = []
    spent = 0
    for model, dims, c in grid:
        need = warmup + trials
        remaining = config.MAX_TOTAL_CALLS - spent
        if remaining <= warmup:
            print(f"  ! call budget ({config.MAX_TOTAL_CALLS}) reached — skipping remaining configs")
            break
        this_trials = trials
        if need > remaining:
            this_trials = max(0, remaining - warmup)
            print(f"  ! clamping trials to {this_trials} to stay under call budget")
        price = config.MODELS[model]["price_per_1m"]
        print(f"  running {model} dims={dims} concurrency={c} trials={this_trials} ...", flush=True)
        res = await run_config(client, model, dims, c, this_trials, warmup, avg_tokens, price)
        results.append(res)
        spent += warmup + this_trials
    print(f"  total billed calls this run: ~{spent}")
    return results


# --------------------------------------------------------------------------- #
# Output: CSV, table, plot
# --------------------------------------------------------------------------- #
def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_csv(results: list[ConfigResult], stamp: str) -> "os.PathLike":
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RESULTS_DIR / f"embedding_latency_{stamp}.csv"
    fields = list(asdict(results[0]).keys()) if results else []
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))
    return path


def _fmt(v: float | None, nd: int = 1) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


def print_table(results: list[ConfigResult]) -> None:
    header = (
        f"{'model':<24}{'dims':>6}{'conc':>6}{'ok':>5}"
        f"{'p50':>9}{'p95':>9}{'p99':>9}{'thru/s':>9}{'429':>6}{'$/1M q':>10}"
    )
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        short = r.model.replace("text-embedding-3-", "3-")
        print(
            f"{short:<24}{r.dims:>6}{r.concurrency:>6}{r.ok:>5}"
            f"{_fmt(r.p50):>9}{_fmt(r.p95):>9}{_fmt(r.p99):>9}"
            f"{r.throughput_per_s:>9.1f}{r.n_429:>6}{r.cost_per_1m_queries:>10.2f}"
        )
    print()


def write_plot(results: list[ConfigResult], stamp: str) -> "os.PathLike | None":
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # noqa: BLE001
        print(f"  ! plot skipped (matplotlib unavailable: {e})")
        return None

    series: dict[str, list[ConfigResult]] = {}
    for r in results:
        series.setdefault(f"{r.model.replace('text-embedding-3-', '3-')}@{r.dims}", []).append(r)

    fig, axes = plt.subplots(1, 3, figsize=(15, 5), sharex=True)
    for ax, (metric, label) in zip(axes, [("p50", "p50"), ("p95", "p95"), ("p99", "p99")]):
        for name, rows in series.items():
            rows = sorted(rows, key=lambda r: r.concurrency)
            xs = [r.concurrency for r in rows]
            ys = [getattr(r, metric) for r in rows]
            ax.plot(xs, ys, marker="o", label=name)
        ax.set_title(f"{label} latency vs concurrency")
        ax.set_xlabel("concurrency")
        ax.set_ylabel("latency (ms)")
        ax.grid(True, alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RESULTS_DIR / f"embedding_latency_{stamp}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Query-embedding latency & cost benchmark")
    ap.add_argument("--dry-run", action="store_true",
                    help="1 model, concurrency 1, few trials, no plot — wiring check")
    ap.add_argument("--models", nargs="*", default=list(config.MODELS),
                    help="subset of model names to run")
    ap.add_argument("--concurrency", type=int, nargs="*", default=config.CONCURRENCY,
                    help="concurrency levels to sweep")
    ap.add_argument("--trials", type=int, default=config.TRIALS,
                    help="timed calls per config")
    ap.add_argument("--warmup", type=int, default=config.WARMUP,
                    help="warmup calls per config (discarded)")
    ap.add_argument("--no-plot", action="store_true", help="skip the plot")
    return ap.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    load_dotenv(config.REPO_ROOT / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not found (looked in "
              f"{config.REPO_ROOT / '.env'} and the environment).", file=sys.stderr)
        return 2

    models = args.models
    concurrency = args.concurrency
    trials = args.trials
    warmup = args.warmup
    plot = not args.no_plot

    if args.dry_run:
        models = models[:1]
        concurrency = [1]
        trials = min(trials, 5)
        warmup = min(warmup, 2)
        plot = False
        print(f"DRY RUN: model={models} concurrency={concurrency} trials={trials}")

    grid = build_grid(models, concurrency)
    print(f"Grid: {len(grid)} configs (models={models}, concurrency={concurrency}, "
          f"trials={trials}, warmup={warmup})")

    client = AsyncOpenAI(timeout=config.TIMEOUT_S)
    try:
        results = await run_grid(client, grid, trials, warmup)
    finally:
        await client.close()

    if not results:
        print("No results produced.", file=sys.stderr)
        return 1

    stamp = timestamp()
    print_table(results)
    csv_path = write_csv(results, stamp)
    print(f"CSV  -> {csv_path}")
    if plot:
        png_path = write_plot(results, stamp)
        if png_path:
            print(f"Plot -> {png_path}")
    return 0


def main() -> int:
    args = parse_args(sys.argv[1:])
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
