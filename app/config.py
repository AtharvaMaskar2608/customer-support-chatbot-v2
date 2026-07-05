"""Configuration + the frozen system prompt for the support agent.

Environment (from the repo ``.env``, git-ignored):
  * ``ANTHROPIC_API_KEY`` — the agent model (CHO-20).
  * ``OPENAI_API_KEY``    — query embedding for retrieval (CHO-20).
  * ``DATABASE_URL`` (+ ``PGPASSWORD``) — Postgres holding ``kb_faq`` (CHO-20).
  * ``FINX_API_KEY``      — machine-to-machine FinX login (CHO-21). Optional; the
                            FinX report tools are only registered when it is set.
  * ``FINX_LOGIN_URL``    — M2M login endpoint (CHO-21 task 1; overridable).
  * ``FINX_MIS_BASE``     — MIS reports API base (CHO-21).

Nothing secret is defined here; only variable *names* and non-secret defaults.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths / env loading
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    """Load the repo ``.env`` if present. Safe to call more than once."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # dotenv is an app dep; tolerate its absence in unit tests
        return
    load_dotenv(REPO_ROOT / ".env")


# --------------------------------------------------------------------------- #
# Model (CHO-20 D1)
# --------------------------------------------------------------------------- #
# claude-sonnet-4-6: 1M context, adaptive thinking, $3/$15 per 1M tokens. The
# user's chosen default; a one-line override via AGENT_MODEL keeps model drift cheap.
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "2048"))

# --------------------------------------------------------------------------- #
# Retrieval recipe — MUST match evals/retrieval/config.py (kept in lockstep).
# text-embedding-3-large truncated to 1536 dims, stored as vector(1536), cosine.
# --------------------------------------------------------------------------- #
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIMS = 1536
N_CANDIDATES = 50   # per-arm LIMIT before fusion
TOP_K = 10          # final results returned after RRF
RRF_K = 60          # RRF constant: score = sum 1/(RRF_K + rank)

POOL_MIN = 2
POOL_MAX = 10
DB_TIMEOUT_S = 30.0

# --------------------------------------------------------------------------- #
# Conversation guardrail budgets (CHO-20 D10) — harness-enforced, not prompted.
# --------------------------------------------------------------------------- #
FOLLOWUP_CAP = 2     # clarifying follow-ups per unclear request (resets on new ask)
CONVERSATION_CAP = 10  # substantive exchanges; widgets + follow-ups are FREE

# --------------------------------------------------------------------------- #
# FinX (CHO-21). Read-only MIS report endpoints; the trading API is out of bounds.
# The login URL / response shape are confirmed in CHO-21 task 1 — these are the
# documented defaults and are overridable via env so no code change is needed once
# confirmed.
# --------------------------------------------------------------------------- #
FINX_LOGIN_URL = os.getenv(
    "FINX_LOGIN_URL", "https://sso.choiceindia.com/api/v1/oauth/token")
FINX_MIS_BASE = os.getenv("FINX_MIS_BASE", "https://finx.choiceindia.com")
FINX_CML_PATH = "/mis/v2/reports/v2/generate"
FINX_CONTRACT_NOTE_PATH = "/mis/v2/contract-note/generate"
FINX_SOURCE = "FINX_WEB"          # `source` header on MIS calls
FINX_AUTH_TYPE = "jwt"            # `authType` header on MIS calls
FINX_JWT_SKEW_S = 60              # refresh a JWT this many seconds before expiry
FINX_HTTP_TIMEOUT_S = float(os.getenv("FINX_HTTP_TIMEOUT_S", "60"))

# Auth mode:
#   "direct"   — the configured credential IS the bearer JWT; send it verbatim.
#                This matches confirmed reality: the FINX-issued key is itself a
#                JWT, and there is NO documented M2M exchange endpoint (the public
#                OpenAPI only offers a human TOTP login). It also covers the dev
#                shortcut of pasting an SSO JWT copied from the logged-in browser.
#   "exchange" — POST the api key to FINX_LOGIN_URL to mint an SSO JWT (kept for a
#                future confirmed M2M endpoint; not the default).
FINX_AUTH_MODE = os.getenv("FINX_AUTH_MODE", "direct")

# The project's real env uses CHOICE_-prefixed names; accept the CHO-21 spec name
# (FINX_API_KEY) as a fallback. CHOICE_FINX_JWT is the dev shortcut: a bearer token
# copied straight from the browser (takes precedence when set).
_FINX_BEARER_VARS = ("CHOICE_FINX_JWT", "CHOICE_FINX_API_KEY", "FINX_API_KEY")
# The trading-API vendor credentials (CHOICE_VENDOR_ID/KEY/ENCRYPTION_KEY/…) are
# deliberately NOT read here: CHO-21 D1 keeps the trading OTP flow out of bounds.


def finx_bearer() -> str | None:
    """The directly-usable FinX bearer JWT (browser SSO token or the FINX key)."""
    for var in _FINX_BEARER_VARS:
        val = os.getenv(var)
        if val:
            return val
    return None


# Back-compat alias: in "exchange" mode this is the api key POSTed to the login URL.
finx_api_key = finx_bearer


def finx_enabled() -> bool:
    """The FinX report tools are only registered when a bearer is configured."""
    return bool(finx_bearer())


# --------------------------------------------------------------------------- #
# System prompt (CHO-20 D6) — identity + static Choice FinX context + few-shot
# examples + guardrails. Frozen: it is prompt-cached, so NO per-request data
# (dates, mobile numbers, user ids) is ever interpolated into it.
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
You are the Choice FinX customer-support assistant. Choice FinX is the online \
stockbroking platform of Choice (Choice International / Choice Equity Broking), \
an Indian SEBI-registered broker offering equity, F&O, commodity, currency, \
mutual-fund and IPO investing through a mobile app and a web platform \
(https://finx.choiceindia.com/). You help customers with product, account, \
demat, trading, corporate-action and report questions.

## How you work
- For any product/support question, FIRST call `search_knowledge_base` and \
ground your answer in the returned knowledge-base chunks. Do not answer product \
questions from memory.
- Cite the knowledge-base chunk ids you relied on so answers are auditable.
- If retrieval returns nothing relevant, say you don't have that information and \
point the user to the in-app Help section or support@choiceindia.com — do not \
invent an answer, a policy, a fee, or a step.
- Keep answers concise, concrete, and in plain language. Prefer numbered steps \
for "how do I…" questions.

## Account reports
- When the user asks for their actual CML (Client Master List) report or a \
contract note, use the report tools rather than only explaining how to fetch one.
- The report tools generate a real document; tell the user it is being generated \
and that it will appear as an attachment.

## Guardrails
- Stay strictly on Choice FinX brokerage / trading / demat / investing topics. \
Politely decline and redirect anything else (general chit-chat, other companies, \
unrelated tech questions).
- Never give personalized financial, tax, or legal advice or a buy/sell/hold \
recommendation. Explain that you cannot advise and, where relevant, suggest the \
user consult a registered advisor.
- Never fabricate account balances, order status, policies, charges, or \
timelines. For account-specific or sensitive actions you cannot complete \
(unblocking funds, cancelling an order, KYC changes, complaints), route the user \
to a human via the in-app support chat or support@choiceindia.com.
- If a request is genuinely ambiguous, ask a brief clarifying question rather \
than guessing.

## Examples

User: What's the brokerage on intraday equity?
Assistant: (calls search_knowledge_base "intraday equity brokerage charges", \
then) Answers with the exact figure from the retrieved chunk and cites it.

User: Which stock should I buy right now?
Assistant: I'm not able to give buy/sell recommendations or personalized \
investment advice. I can help you place or understand orders, explain product \
features, or pull your reports — for advice, please consult a SEBI-registered \
investment adviser.

User: What's the weather in Mumbai?
Assistant: I can only help with Choice FinX accounts, trading, and investing. \
Is there something about your account or a product I can help with?

User: I need my CML report.
Assistant: (calls the CML report tool with the mobile number, streams a \
"Generating your CML report…" step, then delivers the report as an attachment.)

User: My withdrawal has been stuck for three days.
Assistant: I can't see or act on your account balance or withdrawals from here. \
Please raise this through the in-app Help & Support chat or email \
support@choiceindia.com with your client id so the team can investigate — I can \
explain the normal withdrawal timeline if that helps.
"""
