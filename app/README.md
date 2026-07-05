# `app/` — Choice FinX customer-support agent

The first product code in the repo (sibling to `evals/`). A streaming Claude
tool-use agent that answers Choice FinX support questions grounded in `kb_faq`
(**CHO-20**, `support-agent`), plus read-only FinX account-report tools —
**CML report** and **contract note** — layered on top (**CHO-21**,
`finx-report-tools`).

## Layout

| File | What it is |
|------|-----------|
| `config.py` | Env config, model id (`claude-sonnet-4-6`), the frozen system prompt. |
| `events.py` | The typed SSE event model — the core contract. |
| `retrieval.py` | Read-only hybrid-RRF retrieval over `kb_faq` (reuses the CHO-17 recipe) + the `search_knowledge_base` tool schema. |
| `agent.py` | The streaming agentic loop, tool dispatch, widget pause/resume, conversation cap. |
| `server.py` | FastAPI SSE service (`POST /chat`, `GET /health`). |
| `finx/auth.py` | M2M FinX API-key → cached/refreshed SSO JWT (CHO-21). |
| `finx/reports.py` | The two read-only report tools + Claude schemas. See `finx/README.md`. |
| `tests/` | Unit tests (stubbed model + tools + HTTP; no network). |
| `static/index.html` | Thin throwaway test client that renders tokens + a live activity line. |

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt
# Secrets in the repo .env (git-ignored): ANTHROPIC_API_KEY, OPENAI_API_KEY,
# DATABASE_URL (+ PGPASSWORD). For the FinX report tools also add FINX_API_KEY.
uvicorn app.server:app --reload
# then open http://127.0.0.1:8000/static/index.html   (or POST /chat directly)
```

`GET /health` returns `{status, model, finx}` — `finx` is `true` only when
`FINX_API_KEY` is set (otherwise the report tools are simply not registered).

## SSE event contract (`POST /chat`)

Request (the API is **stateless** — the client holds the conversation):

```json
{ "messages": [ {"role":"user","content":"How do I apply for a buyback?"} ],
  "widget_values": { "<correlation_id>": "05-07-2026" } }
```

The response is `text/event-stream`. Each frame is `event: <type>\ndata: <json>\n\n`:

| `event:` | `data` | Meaning |
|----------|--------|---------|
| `token` | `{text}` | An assistant text delta — append it. |
| `tool_use` | `{tool, label, input}` | A step announced **before** it runs (e.g. *"Searching the knowledge base for '…'"*). Render as a live activity line. |
| `tool_result` | `{tool, summary}` | Resolves the activity line (*"Found 5 relevant articles"*). |
| `citations` | `{citations:[{id,topic}]}` | The `kb_faq` chunks the answer used — show as sources. |
| `ui_request` | `{widget, spec, correlation_id, prompt}` | Ask the user for structured input (`date_picker`, `choice`). The turn **pauses**. |
| `report` | `{report_type, summary, url?/content_b64?/payload?}` | A generated FinX report artifact (CHO-21). |
| `done` | `{stop_reason, escalated}` | Terminal success. |
| `error` | `{message}` | Terminal failure (client-safe message). |

### Widgets (`ui_request`) — how the round-trip resolves

Some values must never come from LLM free text (dates; later, identity). The model
can only *trigger* the widget — the risky field is removed from its tool schema
(**Model 2**). When the stream emits a `ui_request`, the turn pauses. The client
re-POSTs the **same** `messages` plus `widget_values: {<correlation_id>: <value>}`;
the server injects the value verbatim and the agent resumes. Widget round-trips do
**not** count toward the conversation cap.

## Guardrails (harness-enforced, CHO-20 D10)

- **Follow-up cap** — at most 2 clarifying follow-ups per unclear request (resets
  on a new ask), then an escalation offer.
- **Conversation cap** — 10 substantive exchanges; widgets + follow-ups are free.
  At the cap the agent winds down gracefully and offers a human via a `choice`
  widget rather than cutting off.

## Tests

```bash
pytest app/tests -q      # 17 tests, no network (stubbed model/tools/HTTP)
```

Live checks performed during development: retrieval against the real `kb_faq`
(RRF SQL + schema) and one end-to-end agent turn against `claude-sonnet-4-6`
(streaming + tool_use + citations + grounded answer). The FinX report path is
covered by unit tests only — it needs a live `FINX_API_KEY` (see `finx/README.md`).
