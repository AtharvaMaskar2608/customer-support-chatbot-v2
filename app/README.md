# Choice FinX Support Agent (CHO-20)

The first application on the RAG substrate validated in CHO-16 → CHO-19: a Claude
tool-use agent that answers Choice FinX brokerage/trading/demat questions grounded in
`kb_faq`, streams its answer **and** its intermediate steps, and enforces support
guardrails. OpenSpec change: `openspec/changes/support-agent/`.

## Layout

```
app/
  config.py      # model (claude-sonnet-4-6), retrieval recipe, caps, .env loader
  retrieval.py   # CHO-17 hybrid-RRF search + the search_knowledge_base tool schema
  events.py      # typed event stream (token / tool_use / tool_result / ui_request / citations / done / error)
  agent.py       # streaming, guarded, non-thinking tool-use loop (the reusable core)
  server.py      # FastAPI SSE service + a thin static test client
  cli.py         # terminal driver (same core, no HTTP)
  tests/         # stubbed-model unit tests for the loop + caps
```

The core (`agent.py`) yields a typed event stream and is import-usable without the web
layer — `server.py` and `cli.py` are two front-ends over the same `SupportAgent.run`.

## Requirements

- Reads `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DATABASE_URL` (+ `PGPASSWORD`) from the
  repo `.env`. Read-only against `kb_faq` (must already be populated with
  `text-embedding-3-large`@1536 embeddings — the app asserts the dims at startup).

```bash
pip install -r app/requirements.txt
```

## Run

```bash
# HTTP service + built-in test client at http://127.0.0.1:8000/
uvicorn app.server:app --reload

# or the CLI
python -m app.cli "Is brokerage charged on square-off trades?"
python -m app.cli                 # interactive REPL

# unit tests (no network / DB)
python -m app.tests.test_agent_loop
```

## HTTP API

### `POST /chat` — stream a turn as SSE

The API is **stateless**: send the whole conversation plus the small counter `state`;
get the updated `state` back on the terminal event.

Request body:
```json
{
  "messages": [{"role": "user", "content": "How do I open a demat account?"}],
  "state": {"exchanges": 0, "followups": 0}
}
```

Response is `text/event-stream`. Event types:

| `event:`      | `data` payload | Meaning |
|---------------|----------------|---------|
| `token`       | `{text}` | An assistant answer delta (stream to the UI). |
| `tool_use`    | `{tool, label, input}` | Emitted **before** a tool runs — `label` is a human-readable status (e.g. *Searching the knowledge base for "…"*). |
| `tool_result` | `{tool, summary, count}` | Short result summary (e.g. *Found 5 relevant articles*). |
| `ui_request`  | `{widget, label, correlation_id, options, purpose, resume_messages}` | The agent needs input; the turn **pauses**. See below. |
| `citations`   | `{citations: [{chunk_id, topic, question}]}` | The `kb_faq` chunks the answer is grounded in. |
| `done`        | `{state, paused}` | Terminal. `state` is the updated counters; `paused=true` means it stopped awaiting widget input, not a completed answer. |
| `error`       | `{message}` | The turn aborted. |

### `GET /health`
Liveness: `{"ok": true, "model": "claude-sonnet-4-6"}`.

## Structured input (`ui_request`) and how to resume

Some values must never be parsed from LLM free text (a date; a choice among fixed
options) and clarifying questions are asked through the same channel. When the model
triggers one, the stream emits a `ui_request` and pauses:

- `widget`: `date_picker` | `choice` | `text`
- `purpose`: `input` (a value), `clarification` (a follow-up question), or
  `escalation` (a Yes/No human-handoff offer)
- `correlation_id`: ties the eventual answer back to the pending request
- `resume_messages`: the conversation to POST back (present for `input` /
  `clarification`; `null` for `escalation`)

**Resuming an `input` / `clarification` widget** — take `resume_messages`, append the
user's selection as a `tool_result` carrying `correlation_id`, and POST again:

```json
{
  "messages": [ ...resume_messages,
    {"role": "user", "content": [
      {"type": "tool_result", "tool_use_id": "<correlation_id>", "content": "Contract Note"}
    ]}
  ],
  "state": {"exchanges": 0, "followups": 1}
}
```

**Resuming an `escalation` offer** — there is no pending tool call; just send the
user's choice as a normal new user message.

## Guardrails (harness-enforced, not the prompt)

- **Grounding:** product questions are answered only from retrieved `kb_faq` chunks,
  with `[KB #<id>]` citations; if the KB lacks an answer, the agent says so and offers
  a human.
- **Scope / advice:** brokerage/trading/demat only; no personalized financial/tax/legal
  advice; no invented policy; account-specific/sensitive actions escalate to a human.
- **Follow-up cap:** at most **2** clarifying follow-ups per unclear request (counter
  resets when the request is resolved), then an escalation offer.
- **Conversation cap:** **10** substantive exchanges, then a graceful wind-down +
  escalation offer. Widget round-trips and clarifying follow-ups do **not** count
  toward this budget. Both caps are explicit counters in `agent.py`, carried in
  `state` — the model never counts its own turns.

## Model

`claude-sonnet-4-6`, **extended thinking disabled** (`thinking={"type":"disabled"}`) —
each assistant turn is a clean text + `tool_use` transcript replayed verbatim across
the loop. Override the model with `AGENT_MODEL`. The static system prompt is
prompt-cached.

## Out of scope (deferred)

Production UI (the static client is a throwaway), auth, rate limiting, conversation
persistence, external tools beyond `search_knowledge_base` (the event model already
supports them — a future `query_account_api` surfaces the same way), and the
generator-quality eval (AnswerRelevancy / Faithfulness) that will consume this agent.
