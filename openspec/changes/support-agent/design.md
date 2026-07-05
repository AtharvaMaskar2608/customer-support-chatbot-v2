## Context

The RAG substrate is built and measured (CHO-16→19): `text-embedding-3-large`@1536 vectors in `kb_faq` (~1,102 rows, 18 topics), a Postgres hybrid RRF query (dense vector + FTS + Reciprocal Rank Fusion), and a validated retrieval quality of ~97% recall@10 / ~75% hit@1 on realistic queries. This change puts that substrate behind a Claude tool-use agent and exposes it as a streaming API. It follows the Anthropic customer-support-agent guide's structure (system prompt with static context + few-shot examples + guardrails, a tool the model calls, an agentic loop) but adapts it to the Choice FinX brokerage domain and adds a hard requirement the guide doesn't cover: **the frontend must see intermediate steps** (tool calls, API queries), not just the final answer.

Model IDs and API shapes below were verified against the current Anthropic model catalog / SDK docs, not recalled.

## Goals / Non-Goals

**Goals:**
- Answer Choice FinX support questions grounded in `kb_faq`, via a `search_knowledge_base` tool the agent calls in an agentic loop.
- Stream to the client both the assistant's answer (token-by-token) **and** intermediate steps (which tool ran, with what query; a short result summary) as typed SSE events.
- Enforce on-topic behavior and support guardrails; cite the `kb_faq` chunks used.
- Ship the agent as a reusable, importable core (`app/`) with a FastAPI SSE service on top.
- Reuse the CHO-17 retrieval recipe verbatim; read-only against `kb_faq`.

**Non-Goals:**
- Production frontend/UI (a thin static test client is the ceiling); auth, rate limiting, conversation persistence.
- Additional external tools beyond `search_knowledge_base` (the event model is generic enough to add them later).
- The generator-quality eval (AnswerRelevancy / Faithfulness) that will consume this agent.
- Any change to the retriever, RRF constants, or `kb_faq`.

## Decisions

**D1 — Model `claude-sonnet-4-6`, thinking disabled, prompt-cached system prompt.**
`claude-sonnet-4-6` (1M context, $3/$15 per 1M tokens) is a strong, cost-effective fit for a RAG support loop and is the user's chosen default (configurable via `AGENT_MODEL`). **Extended thinking is disabled** (`thinking={"type":"disabled"}`) — the user's explicit choice for this agent. Grounded FAQ answering gains little from thinking, and disabling it keeps latency down and, crucially, keeps each assistant turn a clean `text + tool_use` transcript we replay verbatim across the manual loop (no thinking blocks to preserve, no signature/ordering pitfalls). `{"type":"disabled"}` is a valid setting on Sonnet 4.6 (it would otherwise default to adaptive). The large static prompt (identity + Choice FinX context + few-shot examples + guardrails) is stable, so it gets a `cache_control: {"type":"ephemeral"}` breakpoint for ~90% cheaper repeat reads. Tools render before system in the cache prefix, so the tool list stays fixed.

**D2 — Manual streaming agentic loop, not the SDK tool-runner.**
We need to interleave *our own* SSE events (tool-use status, result summaries) between model turns, so we drive the loop by hand with `AsyncAnthropic().messages.stream(...)`: for each turn, stream text deltas out as `token` events; call `stream.get_final_message()`; if `stop_reason == "tool_use"`, emit a `tool_use` status event per tool call, execute the tool, emit a `tool_result` event, append the `tool_result` block, and loop; stop on `end_turn`. The SDK tool-runner hides exactly the seam we need to surface.

**D3 — `search_knowledge_base` reuses the CHO-17 retrieval path.**
Import the `SQL_RRF` (+ arm SQL) and `config` (EMBED_MODEL/DIMS, N_CANDIDATES, TOP_K, RRF_K) from `evals.retrieval` (or lift them into a shared `app/retrieval.py` that both can share — see D8). The tool: embed the query (`text-embedding-3-large`@1536), run `SQL_RRF`, fetch the top-K chunk texts, and return them to the model as a compact list of `{id, topic, chunk}` so it can answer and cite. Read-only; assert embedding dims match `kb_faq` at startup.

**D4 — Typed SSE event model is the core contract.**
The agent core yields an async stream of typed events; the FastAPI layer serializes them as SSE. Event types: `token` (assistant text delta), `tool_use` (`{tool, input}` → rendered as *"Searching the knowledge base for '…'"*), `tool_result` (`{tool, summary}` → *"Found 5 relevant articles"*), `citations` (the `kb_faq` ids/topics the answer drew on), `done` (final stop), `error`. Making the agent yield events (rather than the server reaching into the loop) keeps the core reusable for a CLI, an eval harness, or a different transport.

**D5 — Human-readable intermediate steps, not raw tool JSON.**
Each `tool_use` event carries a short, user-facing label derived from the tool + input (e.g. the search query in quotes), plus the structured input for debugging. This is what satisfies the "show intermediate steps" requirement — the frontend renders a live activity line ("Searching the knowledge base…") that resolves to a result summary. Generic by design so a future `query_account_api` tool surfaces the same way.

**D6 — Guardrails in the system prompt + a light output check.**
The system prompt constrains scope (Choice FinX brokerage/trading/demat only), forbids inventing policy or giving personalized financial/tax/legal advice, requires grounding answers in retrieved chunks (and saying so when the KB lacks an answer), and defines an escalation path for account-specific or sensitive actions (route to a human / official channel). `claude-sonnet-4-6` follows guardrails well; keep instructions firm but not so aggressive they overtrigger. Citations come from the tool's returned chunk ids.

**D7 — FastAPI SSE endpoint.**
`POST /chat` takes `{messages: [...], }` (client holds conversation state — the API is stateless, matching Anthropic's model) and returns `text/event-stream`. Each event is `event: <type>\ndata: <json>\n\n`. A thin static HTML/JS client (optional, in `app/static/`) demonstrates rendering tokens + activity lines. `GET /health` for liveness.

**D8 — Productionize the retriever into `app/retrieval.py`.**
The CHO-17 RRF SQL currently lives inside a benchmark module. To avoid the app importing an `evals/` benchmark at runtime, lift the SQL + embedding helper into `app/retrieval.py` as the single source of truth (a small, clean `retrieve(query) -> [chunk]`), and keep the constants aligned with `evals/retrieval/config.py`. This is the one piece of "shared library" the app needs; it stays read-only and dependency-light.

**D9 — Structured input via UI widgets (`ui_request` event); the LLM never parses risky values.**
Some values must not come from LLM free-text — dates (`01-07` is ambiguous; "last Friday" needs a calendar; the model can hallucinate one) and, later, account/identity. The agent requests these through a generic **`ui_request`** SSE event carrying a widget spec (`date_picker`, `choice`/buttons, …); the frontend renders it, the user picks, and a **structured value** returns as the next input. Enforcement is **harness-side (Model 2)**: the risky field is *removed from the LLM tool schema entirely*, so the model can only *trigger* the widget, never fill the value. Same "the tool is the boundary, not the prompt" principle as identity. One primitive covers date pickers, choice-button follow-ups, and the escalation Yes/No.

**D10 — Conversation guardrails, harness-enforced (not prompt-enforced).**
Two counters live in the loop, not the prompt (the model can't reliably count):
- **Follow-up cap = 2 per unclear request**, and the counter **resets whenever the user asks something new**. After the 2nd unsuccessful clarification, the agent offers to escalate ("connect you to a human?") and the *user* decides.
- **Conversation cap = 10 substantive exchanges** (a user ask → an answer). At the cap, the agent gives a graceful wrap-up and offers the same human escalation — no hard cut-off.
- **Widget round-trips and clarifying follow-ups are FREE** — they do NOT count toward the 10. So "10" means *10 distinct things the user actually wanted*; all clarification/structured-input machinery is unbounded by the budget (follow-ups are bounded only by their own 2-per-request cap). The two escalation triggers (stuck after 2 follow-ups; out of budget at 10) share one offer, which itself can be a free Yes/No widget.

## Risks / Trade-offs

- **SSE + async streaming complexity.** Interleaving token deltas with tool-status events across an async generator and a manual tool loop is the trickiest part. → Define the event model first (D4), unit-test the loop with a stubbed model + stubbed tool before wiring FastAPI, and keep the server layer a thin serializer.
- **Latency of the tool round-trip is visible to the user.** Each search adds an embedding call + SQL. → This is exactly why intermediate-step events exist: the "Searching…" line makes the wait legible instead of a dead pause. Reuse a warm `asyncpg` pool and a single `AsyncOpenAI` client.
- **Grounding / hallucination.** The agent might answer from prior knowledge instead of the KB. → System prompt requires tool use for product questions and grounding in returned chunks; hit@1 is only ~75%, so return top-K (5–10) and let the model select; cite chunk ids so answers are auditable. A later Faithfulness eval quantifies this.
- **Guardrail overtrigger or leakage.** Too-aggressive scope rules refuse valid questions; too-loose ones give advice. → Few-shot examples show the desired boundary behavior; tune with the QA loop after a dogfood pass.
- **Prompt-cache invalidation.** Any edit to the static prompt or tool list busts the cache. → Freeze the static prompt as a module constant; never interpolate per-request data (dates, user ids) into it.
- **Model-ID / SDK drift.** → `claude-sonnet-4-6` and the streaming/tool-use shapes were verified against the current catalog; `AGENT_MODEL` makes the model a one-line change.
- **Scope creep toward a real frontend.** → Explicitly a non-goal; the deliverable is the SSE contract + a throwaway test client, not a product UI.
- **Turn-counting ambiguity.** "What counts as a to-and-fro" is easy to get wrong. → Precise rule: a *substantive user ask → answer* counts; **internal tool calls, widget round-trips, and clarifying follow-ups do NOT count** toward the 10 (follow-ups are governed only by the 2-per-request cap). Enforce with an explicit counter in the loop and unit-test the boundaries.
- **Widget round-trip state.** A returned structured value (e.g. a picked date) must be re-associated with the pending intent. → The `ui_request` carries a correlation id echoed back in the response; the stateless client passes it in the next request.

## Open Questions

_Resolved:_ interface = **core lib + FastAPI (SSE)**; model = **`claude-sonnet-4-6`**; intermediate tool/API steps **must** stream to the client (typed `tool_use`/`tool_result` events). No Linear ticket exists yet for this work.
