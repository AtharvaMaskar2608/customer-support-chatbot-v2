"""Configuration for the Choice FinX support agent (CHO-20).

Single home for the knobs the app needs at runtime. The retrieval recipe here is
lifted from the CHO-17 benchmark (``evals/retrieval/config.py``) verbatim — it MUST
match the recipe that populated ``kb_faq.embedding`` (``text-embedding-3-large``
truncated to 1536 dims, cosine) or recall collapses. The app is the single source of
truth for the recipe at runtime; it does not import an ``evals/`` benchmark.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# --------------------------------------------------------------------------- #
# Model — claude-sonnet-4-6 (1M context, adaptive thinking, $3/$15 per 1M).
# Overridable via AGENT_MODEL so the same core can be pointed at another model.
# --------------------------------------------------------------------------- #
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "2048"))

# --------------------------------------------------------------------------- #
# Retrieval recipe (must match kb_faq.embedding — see CHO-16/CHO-17).
# --------------------------------------------------------------------------- #
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIMS = 1536

N_CANDIDATES = 50   # per-arm LIMIT before RRF fusion
TOP_K = 8           # final KB articles handed to the model per search
RRF_K = 60          # RRF constant: score = sum 1/(RRF_K + rank)

# --------------------------------------------------------------------------- #
# Connection pool + timeouts.
# --------------------------------------------------------------------------- #
POOL_MIN = int(os.getenv("AGENT_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("AGENT_POOL_MAX", "10"))
OPENAI_TIMEOUT_S = 30.0
ANTHROPIC_TIMEOUT_S = 120.0

# --------------------------------------------------------------------------- #
# Conversation guardrails — harness-enforced counters (design D10), NOT prompt.
# --------------------------------------------------------------------------- #
MAX_FOLLOWUPS = 2   # clarifying follow-ups per unclear request, then escalate
MAX_EXCHANGES = 10  # substantive user-ask/answer exchanges, then wind down
# Safety backstop on tool-use rounds inside a single turn (prevents runaway loops).
MAX_TOOL_ROUNDS = 6

# Repo root — used to locate the shared .env regardless of the working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    """Load the shared repo ``.env``.

    Prefers ``<repo-root>/.env``; falls back to walking up from the CWD so it also
    works when running inside a git worktree (whose root has no ``.env`` — the file
    lives in the main checkout a few directories up).
    """
    local = REPO_ROOT / ".env"
    if local.exists():
        load_dotenv(local)
    else:
        found = find_dotenv(usecwd=True)
        if found:
            load_dotenv(found)


def require_env() -> tuple[str, str]:
    """Return (DATABASE_URL, source) after asserting the mandatory keys are set.

    OPENAI_API_KEY and ANTHROPIC_API_KEY are read implicitly by their SDK clients;
    we assert them here so misconfiguration fails loudly at startup, not mid-stream.
    """
    dsn = os.getenv("DATABASE_URL")
    missing = [k for k in ("DATABASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
               if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"missing required env vars: {', '.join(missing)} (repo .env)")
    return dsn, "env"
