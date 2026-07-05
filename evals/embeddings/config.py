"""Configuration for the query-embedding latency & cost benchmark (CHO-16)."""
from __future__ import annotations

from pathlib import Path

# Models under test.
#   dims          : output dimensions to sweep for this model. `large` sweeps the
#                   Matryoshka truncations; `small` stays at its native 1536.
#   price_per_1m  : OpenAI synchronous list price per 1M tokens. The Batch API is
#                   ~half price but async (up to 24h) and thus irrelevant to live
#                   query embedding, so it is deliberately not used here.
MODELS: dict[str, dict] = {
    "text-embedding-3-small": {"dims": [1536], "price_per_1m": 0.02},
    "text-embedding-3-large": {"dims": [3072, 1536, 256], "price_per_1m": 0.13},
}

# Concurrency levels = number of simultaneous in-flight requests to sweep.
CONCURRENCY: list[int] = [1, 5, 10, 25, 50]

TRIALS: int = 100          # timed calls per config, after warmup
WARMUP: int = 10           # warmup calls per config (timings discarded)
MAX_TOTAL_CALLS: int = 20_000   # hard safety cap on total billed calls per run
TIMEOUT_S: float = 30.0    # per-request timeout

# Bounded retry with jitter on 429 / transient errors. The raw 429 count is still
# recorded as the rate-limit-ceiling signal (decision: locked).
MAX_RETRIES: int = 4
RETRY_BASE_S: float = 0.5  # backoff sleep = RETRY_BASE_S * 2**attempt + U(0, RETRY_BASE_S)

# tiktoken encoding used for local cost estimation.
TOKEN_ENCODING: str = "cl100k_base"

# Output location for CSV + plot artifacts.
RESULTS_DIR: Path = Path(__file__).resolve().parent / "results"

# Repo root, used to locate the .env file regardless of the working directory.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
