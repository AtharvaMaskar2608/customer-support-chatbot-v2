## Why

The evals arc (CHO-16→19) validated the RAG substrate: embeddings, hybrid RRF retrieval, and retrieval quality (97% recall@10). Nothing yet *uses* it. This change builds the first real application — a **customer-support agent** for the Choice FinX brokerage that answers product questions grounded in `kb_faq`, stays on topic, and works through tool calls in an agentic loop. It turns the CHO-17 hybrid search + RRF into a reusable **`search_knowledge_base` tool** the agent calls, and streams both the answer *and* its intermediate steps (searching the knowledge base, querying an API) to the frontend in real time.

This is the "future `src/`" the evals deliberately never touched — the first product code in the repo.

## What Changes

- Add a new importable **`app/`** package (the agent core), sibling to `evals/`:
  - **Retrieval tool** — wrap the CHO-17 hybrid RRF SQL + `text-embedding-3-large`@1536 recipe as a `search_knowledge_base(query, top_k)` function and its Claude tool schema; returns top-K `kb_faq` chunks with `id`/`topic` for grounding and citations. Read-only.
  - **Agent loop** — a streaming, manual Anthropic tool-use loop on **`claude-sonnet-4-6`**: stream each turn's text, detect `tool_use` blocks, execute the tool, feed results back, repeat until `end_turn`. Adaptive thinking on; the large static prompt (identity + Choice FinX context + few-shot examples + guardrails) is prompt-cached.
  - **System prompt / config** — domain-adapted from the Anthropic support-agent guide: identity, static Choice FinX product context, 4–5 example interactions, and guardrails (stay on brokerage/trading/demat topics, no financial advice, don't invent policy, cite sources, escalate to a human for account-specific or sensitive actions).
  - **SSE event model** — a typed event stream so the frontend renders progressively.
- Add a **FastAPI** service exposing `POST /chat` as **Server-Sent Events (SSE)**, streaming a typed event sequence: `token` (assistant text deltas), `tool_use` (intermediate status, e.g. *"Searching the knowledge base for 'square-off brokerage'"*), `tool_result` (e.g. *"Found 5 relevant articles"*), `citations`, `done`, and `error`. Multi-turn conversation state passed by the client.
- Add app-scoped dependencies (`anthropic`, `fastapi`, `uvicorn`, `sse-starlette` or native SSE) in a new `app/requirements.txt`.

Non-goals (deferred): a production frontend/UI (SSE contract is defined; a thin HTML/JS test client is the most that's in scope); auth / rate limiting / persistence of conversations; real external "query API" tools beyond the retrieval tool (the event model supports them, but only `search_knowledge_base` ships now); the generator-quality eval (AnswerRelevancy / Faithfulness) that will consume this agent — a follow-up.

## Capabilities

### New Capabilities
- `knowledge-retrieval-tool`: A reusable, read-only hybrid-RRF retrieval function over `kb_faq` (reusing the CHO-17 SQL + embedding recipe) plus its Claude tool schema, returning ranked chunks with ids/topics for grounding and citations.
- `support-agent`: A streaming Claude tool-use agent (`claude-sonnet-4-6`) that answers Choice FinX support questions grounded in the knowledge base, enforces on-topic/guardrail behavior, and drives the `search_knowledge_base` tool in an agentic loop.
- `agent-sse-api`: A FastAPI Server-Sent-Events endpoint that streams the agent's answer token-by-token **and** surfaces intermediate steps (tool calls / API queries) to the client as typed events.

### Modified Capabilities
<!-- None. New application area; the eval capabilities (embedding-benchmark, hybrid-retrieval-benchmark, golden-set-generation, retrieval-quality-eval) are unaffected. -->

## Impact

- **New directory**: `app/` (`config.py`, `retrieval.py`, `agent.py`, `events.py`, `server.py`, `requirements.txt`, optional `static/` test client).
- **Reuses**: the CHO-17 RRF SQL + embedding recipe (`evals/retrieval`), the Claude wiring pattern from `evals/quality/claude_model.py`, the `kb_faq` table (read-only).
- **Dependencies**: `anthropic`, `fastapi`, `uvicorn[standard]`, `openai` (query embedding), `asyncpg`, `python-dotenv`. New `app/requirements.txt`.
- **Config/secrets**: reads `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DATABASE_URL` (+ `PGPASSWORD`) from `.env` (git-ignored).
- **External calls**: per user turn — 1 query embedding + N Claude streaming calls (N = tool-use rounds + 1) + hybrid RRF SQL. Read-only against `kb_faq`.
- **Model**: `claude-sonnet-4-6` (1M context, adaptive thinking, $3/$15 per 1M tokens) — configurable via `AGENT_MODEL`.
- **Linear**: first application milestone; consumes CHO-17 retrieval and the CHO-18 knowledge base; unblocks a future generator-quality eval.
