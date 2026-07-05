"""The Choice FinX support agent — a streaming, guarded, grounded tool-use loop (CHO-20).

Follows the Anthropic customer-support-agent structure (identity + static context +
few-shot examples + guardrails as the system prompt; tools the model calls; an
agentic loop), adapted to the Choice FinX brokerage/trading/demat domain.

The loop is written MANUALLY on ``messages.stream(...)`` rather than the SDK tool
runner, because the runner hides exactly the seam where intermediate steps surface.
Driving it by hand lets us emit ``token`` deltas AND ``tool_use`` / ``tool_result`` /
``ui_request`` progress events for the frontend. Extended thinking is disabled so each
assistant turn is a clean text + ``tool_use`` transcript we replay verbatim.

Harness-enforced guardrails (design D9/D10) live in this loop, not the prompt:
  * ``ask_clarification`` / ``collect_input`` are the boundary for follow-ups and
    risky values — the model can *trigger* a widget but the value comes from the user.
  * a follow-up cap (2 per unclear request) and a conversation cap (10 substantive
    exchanges) are explicit counters; the model never counts its own turns.

The API is stateless: the caller passes the full ``messages`` list and a small
``state`` dict (counters); this core echoes updated state on the terminal event and,
on a widget pause, the conversation to resume from.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from . import config
from .events import (
    AgentEvent, CitationsEvent, DoneEvent, ErrorEvent, TokenEvent,
    ToolResultEvent, ToolUseEvent, UiRequestEvent,
)
from .retrieval import SEARCH_TOOL, Retriever, format_results_for_model

# --------------------------------------------------------------------------- #
# Harness-owned tools (besides search). The model can call these to ask the user
# something, but their *values* are collected from the user, never invented — the
# tool is the boundary (design D9). ``ask_clarification`` is a capped follow-up;
# ``collect_input`` is a free structured-input widget.
# --------------------------------------------------------------------------- #
CLARIFY_TOOL = {
    "name": "ask_clarification",
    "description": (
        "Ask the user ONE short clarifying question when their request is too "
        "ambiguous to answer or to search effectively. Provide 2–4 `options` when the "
        "clarification is a choice between known alternatives (shown as buttons). Use "
        "sparingly — only when you genuinely cannot proceed. Do not use this to collect "
        "dates or other exact values (use collect_input for those)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The clarifying question."},
            "options": {
                "type": "array", "items": {"type": "string"},
                "description": "Optional discrete choices to offer as buttons.",
            },
        },
        "required": ["question"],
    },
}

COLLECT_INPUT_TOOL = {
    "name": "collect_input",
    "description": (
        "Request a value from the user that must NOT be typed as free text — a "
        "calendar date, or a choice among fixed options. Specify the widget `kind` and "
        "a `label`; the user picks in a widget and the exact value is returned to you. "
        "Never guess or parse these values yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string", "enum": ["date_picker", "choice"],
                "description": "Widget type to render.",
            },
            "label": {"type": "string", "description": "Prompt shown by the widget."},
            "options": {
                "type": "array", "items": {"type": "string"},
                "description": "Choices — required when kind is 'choice'.",
            },
        },
        "required": ["kind", "label"],
    },
}

_TOOLS = [SEARCH_TOOL, CLARIFY_TOOL, COLLECT_INPUT_TOOL]

_ESCALATION_OPTIONS = ["Yes, connect me to a human", "No, thanks"]

# --------------------------------------------------------------------------- #
# System prompt: identity + static context + few-shot + guardrails.
# Frozen constant (no per-request interpolation) so it stays prompt-cached.
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
You are Fin, the customer-support assistant for Choice FinX — an Indian brokerage \
offering equity/derivatives trading, demat accounts, mutual funds, and related \
services. You help customers with product questions grounded in the Choice FinX \
knowledge base.

# How you work
- For any product question, FIRST call `search_knowledge_base` to retrieve relevant \
KB articles, THEN answer using only what those articles say. Never answer product \
questions from prior knowledge.
- If the first search doesn't cover the question, search again with different keywords \
before concluding you don't have the information.
- Ground every factual claim in the retrieved articles. Cite the KB article(s) you \
used by their chunk id in the form `[KB #<chunk_id>]` at the end of the relevant \
sentence.
- Be concise and direct. Lead with the answer, then any steps or detail. Use short \
bullet lists for procedures.

# Asking the user for input
- If a request is genuinely ambiguous, call `ask_clarification` with one focused \
question (offer `options` when the choice is between known alternatives). Don't \
over-ask — prefer searching and answering.
- If you need a value that must be exact and must not be guessed (a specific date, or \
a pick from fixed options), call `collect_input` — never invent the value.

# Guardrails (do not break these)
- Stay strictly on Choice FinX brokerage / trading / demat / mutual-fund topics. For \
anything off-topic, politely decline and steer back.
- Do NOT give personalized financial, investment, tax, or legal advice, or \
recommendations to buy/sell specific securities. Explain what Choice FinX offers and \
how to use it; for suitability decisions, tell the customer to consult a qualified \
advisor.
- Never invent policies, charges, timelines, or features. If the knowledge base does \
not contain the answer, say so plainly and offer to connect the customer to a human \
support agent — do not guess.
- Do not ask for or handle passwords, OTPs, full card/bank numbers, or other \
sensitive credentials. For account-specific actions (viewing balances, unblocking an \
account, disputing a charge), explain the general process from the KB and escalate to \
a human agent for anything requiring account access.

# Examples
User: What are the charges for an equity delivery trade?
Fin: (calls search_knowledge_base "equity delivery brokerage charges", then answers \
from the retrieved article and cites it, e.g. "Equity delivery is brokerage-free; you \
still pay statutory charges like STT and exchange fees. [KB #123]")

User: Which stock should I buy right now?
Fin: I can't recommend specific securities or give personalized investment advice — \
that depends on your goals and risk profile, so please consult a qualified financial \
advisor. I can help with how to place an order or what charges apply on Choice FinX.

User: How do I get my report?
Fin: (calls ask_clarification "Which report do you need?" with options ["Contract \
note", "CML / holding statement", "P&L statement"], then searches and answers once the \
user picks.)

User: Show me my ledger for a specific day.
Fin: (calls collect_input kind="date_picker" label="Pick the date for your ledger" so \
the exact date comes from a calendar, not guessed — then explains the KB process and \
escalates for the account-specific lookup.)

User: What's the weather today?
Fin: I can only help with Choice FinX — brokerage, trading, demat, and mutual-fund \
questions. Is there something about your Choice FinX account I can help with?
"""


def _serialize_assistant(content) -> list[dict]:
    """Minimal, resend-safe serialisation of a response's content blocks.

    Keeps only the fields the Messages API accepts back as input (avoids echoing
    response-only fields like ``citations: null`` that can trip validation).
    """
    out: list[dict] = []
    for block in content:
        if block.type == "text":
            out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append({"type": "tool_use", "id": block.id,
                        "name": block.name, "input": block.input})
    return out


def _is_new_user_ask(messages: list[dict]) -> bool:
    """True when the last message is a fresh user ask (plain text), not a resume
    (a user turn carrying tool_result blocks from a widget/clarification)."""
    if not messages or messages[-1].get("role") != "user":
        return False
    content = messages[-1].get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return True


class SupportAgent:
    """Reusable core. Construct with an Anthropic client + a Retriever; call ``run``."""

    def __init__(self, anthropic: AsyncAnthropic, retriever: Retriever,
                 model: str | None = None, max_tokens: int | None = None) -> None:
        self._anthropic = anthropic
        self._retriever = retriever
        self._model = model or config.AGENT_MODEL
        self._max_tokens = max_tokens or config.MAX_TOKENS

    async def run(self, messages: list[dict],
                  state: dict | None = None) -> AsyncIterator[AgentEvent]:
        """Drive one turn over the full ``messages`` list, yielding a typed stream.

        ``messages`` is the entire Anthropic conversation the (stateless) client holds,
        ending either in a new user ask or in a ``tool_result`` that resumes a paused
        widget/clarification. ``state`` carries the harness counters
        (``exchanges`` / ``followups``); the updated state rides on the terminal event.
        """
        state = {"exchanges": 0, "followups": 0, **(state or {})}
        exchanges = int(state.get("exchanges", 0))
        followups = int(state.get("followups", 0))
        messages = list(messages)

        try:
            # Conversation cap (harness-enforced) — wind down before answering an
            # 11th substantive ask. Resumes (tool_result turns) are allowed through.
            if exchanges >= config.MAX_EXCHANGES and _is_new_user_ask(messages):
                async for ev in self._wind_down(exchanges, followups):
                    yield ev
                return

            citations: list[dict] = []
            seen_ids: set[int] = set()

            for _round in range(config.MAX_TOOL_ROUNDS):
                async with self._anthropic.messages.stream(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=[{"type": "text", "text": SYSTEM_PROMPT,
                             "cache_control": {"type": "ephemeral"}}],
                    tools=_TOOLS,
                    tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                    thinking={"type": "disabled"},
                    messages=messages,
                ) as stream:
                    async for event in stream:
                        if (event.type == "content_block_delta"
                                and event.delta.type == "text_delta"):
                            yield TokenEvent(event.delta.text)
                    final = await stream.get_final_message()

                messages.append({"role": "assistant",
                                 "content": _serialize_assistant(final.content)})

                if final.stop_reason != "tool_use":
                    # Substantive answer — surface citations, count it, reset follow-ups.
                    if citations:
                        yield CitationsEvent(citations)
                    exchanges += 1
                    followups = 0
                    yield DoneEvent(state={"exchanges": exchanges, "followups": 0})
                    return

                # disable_parallel_tool_use → at most one tool_use block per turn.
                tool = next(b for b in final.content if b.type == "tool_use")

                if tool.name == "search_knowledge_base":
                    query = (tool.input or {}).get("query", "")
                    top_k = (tool.input or {}).get("top_k")
                    yield ToolUseEvent(
                        tool="search_knowledge_base",
                        label=f"Searching the knowledge base for “{query}”",
                        tool_input={"query": query, **({"top_k": top_k} if top_k else {})},
                    )
                    results = await self._retriever.search(query, top_k)
                    yield ToolResultEvent(
                        tool="search_knowledge_base",
                        summary=(f"Found {len(results)} relevant article"
                                 f"{'' if len(results) == 1 else 's'}"),
                        count=len(results),
                    )
                    for r in results:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            citations.append({"chunk_id": r["id"], "topic": r["topic"],
                                              "question": r["question"]})
                    messages.append({"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": tool.id,
                        "content": format_results_for_model(results)}]})
                    continue  # loop for the next model turn

                if tool.name == "collect_input":
                    # Free structured-input widget — pause; value comes from the user.
                    kind = (tool.input or {}).get("kind", "choice")
                    yield UiRequestEvent(
                        widget=kind,
                        label=(tool.input or {}).get("label", "Please provide a value"),
                        correlation_id=tool.id,
                        options=(tool.input or {}).get("options"),
                        purpose="input",
                        resume_messages=messages,
                    )
                    yield DoneEvent(
                        state={"exchanges": exchanges, "followups": followups},
                        paused=True)
                    return

                if tool.name == "ask_clarification":
                    followups += 1
                    if followups > config.MAX_FOLLOWUPS:
                        # Cap hit — don't ask again; answer the pending tool call with
                        # an instruction to offer escalation, and let the model phrase it.
                        messages.append({"role": "user", "content": [{
                            "type": "tool_result", "tool_use_id": tool.id,
                            "content": ("Clarifying-question limit reached for this "
                                        "request. Do not ask another question. Briefly "
                                        "apologise for not pinning it down and offer to "
                                        "connect the user to a human support agent.")}]})
                        async for ev in self._finish_with_escalation(messages, exchanges):
                            yield ev
                        return
                    # Within cap — pause and let the user answer the follow-up.
                    q = (tool.input or {}).get("question", "Could you clarify?")
                    opts = (tool.input or {}).get("options")
                    yield UiRequestEvent(
                        widget="choice" if opts else "text",
                        label=q,
                        correlation_id=tool.id,
                        options=opts,
                        purpose="clarification",
                        resume_messages=messages,
                    )
                    yield DoneEvent(
                        state={"exchanges": exchanges, "followups": followups},
                        paused=True)
                    return

                # Unknown tool — return an error result and continue.
                messages.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": tool.id,
                    "content": f"Unknown tool: {tool.name}", "is_error": True}]})

            # Safety backstop — too many tool rounds in one turn.
            yield ErrorEvent("Tool-use loop exceeded the per-turn limit.")

        except Exception as exc:  # noqa: BLE001 — surface any failure as an SSE error
            yield ErrorEvent(f"{type(exc).__name__}: {exc}")

    # ---- harness-generated closers (no pending model tool call) ------------- #
    async def _wind_down(self, exchanges: int, followups: int) -> AsyncIterator[AgentEvent]:
        """Conversation cap reached — graceful wrap-up + escalation offer."""
        msg = ("We've covered a lot in this chat. To make sure you get thorough help "
               "from here, I'd suggest connecting you with a human support agent. "
               "Would you like me to do that?")
        yield TokenEvent(msg)
        yield UiRequestEvent(
            widget="choice", label="Connect you to a human support agent?",
            correlation_id="escalation", options=_ESCALATION_OPTIONS,
            purpose="escalation", resume_messages=None)
        yield DoneEvent(state={"exchanges": exchanges, "followups": followups},
                        paused=True)

    async def _finish_with_escalation(self, messages: list[dict],
                                      exchanges: int) -> AsyncIterator[AgentEvent]:
        """After the follow-up cap: let the model stream its escalation offer, then a
        choice widget. The unclear request is closed, so follow-ups reset to 0."""
        async with self._anthropic.messages.stream(
            model=self._model, max_tokens=self._max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            tools=_TOOLS,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            thinking={"type": "disabled"},
            messages=messages,
        ) as stream:
            async for event in stream:
                if (event.type == "content_block_delta"
                        and event.delta.type == "text_delta"):
                    yield TokenEvent(event.delta.text)
            await stream.get_final_message()
        yield UiRequestEvent(
            widget="choice", label="Connect you to a human support agent?",
            correlation_id="escalation", options=_ESCALATION_OPTIONS,
            purpose="escalation", resume_messages=None)
        yield DoneEvent(state={"exchanges": exchanges, "followups": 0}, paused=True)
