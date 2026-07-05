"""Configuration for the hybrid-retrieval latency benchmark (CHO-17)."""
from __future__ import annotations

from pathlib import Path

# Query embedding — MUST match the recipe that populated kb_faq.embedding:
# text-embedding-3-large truncated to 1536 dims, stored as vector(1536), cosine.
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIMS = 1536

# Retrieval grid (fixed — these do not materially affect latency at ~1.1k rows).
N_CANDIDATES = 50   # per-arm LIMIT before fusion
TOP_K = 10          # final results returned after RRF
RRF_K = 60          # RRF constant: score = sum 1/(RRF_K + rank)

QUERY_SET_SIZE = 100   # queries sampled from kb_faq.question
TRIALS = 100           # timed calls per (pass, arm) after warmup
WARMUP = 10            # discarded

# Pass C — concurrency levels. Pool max must be >= max(CONCURRENCY).
CONCURRENCY = [1, 10, 25]
POOL_MIN = 4
POOL_MAX = 30

TIMEOUT_S = 30.0

RESULTS_DIR = Path(__file__).resolve().parent / "results"
REPO_ROOT = Path(__file__).resolve().parents[2]
