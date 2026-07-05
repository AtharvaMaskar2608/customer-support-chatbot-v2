## 1. Scaffold + dependencies

- [ ] 1.1 Create `app/` package: `__init__.py`, `config.py`, `retrieval.py`, `agent.py`, `events.py`, `server.py`, `requirements.txt`
- [ ] 1.2 Add deps to `app/requirements.txt`: `anthropic`, `fastapi`, `uvicorn[standard]`, `openai`, `asyncpg`, `python-dotenv` (+ `sse-starlette` if used); install into venv
- [ ] 1.3 Confirm `claude-sonnet-4-6` streaming + tool-use works with one tiny `messages.stream` call before building the loop

## 2. Retrieval tool (reuse CHO-17)

- [ ] 2.1 Lift the RRF SQL + embedding recipe into `app/retrieval.py` as `retrieve(query, top_k) -> list[chunk]` (keep constants aligned with `evals/retrieval/config.py`); read-only
- [ ] 2.2 Connect via `asyncpg` pool (`DATABASE_URL`/`PGPASSWORD` from `.env`); assert `vector_dims(kb_faq.embedding) == EMBED_DIMS` at startup
- [ ] 2.3 Return top-K chunks as `{id, topic, chunk}`; embed query with `text-embedding-3-large`@1536 via a shared `AsyncOpenAI` client
- [ ] 2.4 Define the `search_knowledge_base` Claude tool schema (query + optional top_k) with a when-to-use description

## 3. System prompt / config

- [ ] 3.1 Write the Choice FinX identity + static product context (from `kb_faq` domain) in `config.py`
- [ ] 3.2 Add 4–5 few-shot example interactions showing grounded answers, on-topic redirects, and escalation
- [ ] 3.3 Add guardrails (brokerage/trading/demat only; no personalized financial/tax/legal advice; no invented policy; cite sources; escalate account-specific/sensitive actions)
- [ ] 3.4 Assemble the static prompt as a frozen constant with a prompt-cache breakpoint; expose `AGENT_MODEL` (default `claude-sonnet-4-6`)

## 4. Event model + streaming agent loop

- [ ] 4.1 Define typed SSE events in `events.py`: `token`, `tool_use`, `tool_result`, `citations`, `done`, `error` (each with a `to_sse()` serializer)
- [ ] 4.2 Implement the async agent loop in `agent.py` using `AsyncAnthropic().messages.stream(...)`: yield `token` per text delta; on `stop_reason == "tool_use"` yield a `tool_use` step (human-readable label + input) BEFORE executing
- [ ] 4.3 Execute the tool, yield a `tool_result` summary (e.g. "Found N articles"), append the tool_result block, loop until `end_turn`
- [ ] 4.4 Emit `citations` (kb_faq ids/topics used) and a terminal `done`; wrap failures as an `error` event
- [ ] 4.5 Adaptive thinking on; multi-turn state taken from the request (stateless core)

## 5. FastAPI SSE service

- [ ] 5.1 `POST /chat` accepts `{messages}` and returns `text/event-stream`, serializing the core's event stream to SSE (`event:`/`data:` frames)
- [ ] 5.2 `GET /health` liveness; construct the asyncpg pool + Anthropic/OpenAI clients on startup, close on shutdown
- [ ] 5.3 Optional thin `app/static/` test client that renders streamed tokens + a live activity line for tool steps

## 5b. Structured input (widgets) + conversation guardrails

- [ ] 5b.1 Add a `ui_request` SSE event (widget spec: `date_picker`, `choice`/buttons) with a correlation id; pause the turn awaiting a structured selection, resume on return (widgets do NOT count toward the turn budget)
- [ ] 5b.2 Follow-up cap: at most 2 clarifying follow-ups per unclear request, counter resets on a new user ask; after 2, offer human escalation (user decides)
- [ ] 5b.3 Conversation cap: 10 substantive exchanges (widgets + follow-ups excluded), harness-enforced counter; graceful wrap-up + escalation offer at the cap
- [ ] 5b.4 Render clarifying follow-ups and the escalation prompt as `choice` widgets where it helps (buttons, not free text)

## 6. Verify

- [ ] 6.1 Unit-test the agent loop with a stubbed model + stubbed tool: asserts token/tool_use/tool_result/citations/done ordering
- [ ] 6.2 Dry run against a real question end-to-end: confirm grounded answer, a visible "Searching the knowledge base…" step, and citations
- [ ] 6.3 Guardrail spot-checks: off-topic redirect, KB-miss ("I don't have that"), and an escalation case
- [ ] 6.4 Cap/widget checks: 2-follow-up reset behavior, 10-exchange wind-down + escalation offer, and that widgets/follow-ups don't consume the budget
- [ ] 6.5 Document run instructions (`uvicorn app.server:app`) and the SSE event contract (incl. `ui_request`) in `app/README.md`
