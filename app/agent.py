"""Streaming Claude tool-use agent (CHO-20 ``support-agent``).

A manual, streaming agentic loop over ``AsyncAnthropic().messages.stream(...)`` so
we can interleave our OWN typed events (tool-use status, tool results, report
artifacts, widget requests) between model turns — the SDK tool-runner hides
exactly that seam (CHO-20 D2). The core is transport-agnostic: it *yields*
:class:`~app.events.Event` objects; ``app.server`` serializes them to SSE.

Widget round-trips (CHO-20 D9, the ``date_picker`` for contract notes) use the
async-generator **send protocol**: the loop ``yield``s a :class:`UIRequestEvent`
and the consumer sends the selected value back in with ``agen.asend(value)``. This
models pause/resume cleanly and keeps the risky value out of the LLM tool schema
(Model 2). The consumer contract is documented in :func:`drive` below.

Heavy deps are lazy: the only hard import is the event model, so the loop is
unit-testable with a stub client + stub tools and no network.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator

from . import config
from .events import (
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    Event,
    ReportEvent,
    TokenEvent,
    ToolResultEvent,
    ToolUseEvent,
    UIRequestEvent,
)
from .retrieval import SEARCH_TOOL

# FinX tool schemas are imported lazily inside _tools() so importing the agent
# never requires the finx sub-package to be import-clean.


def count_substantive_exchanges(messages: list[dict]) -> int:
    """Count completed user-ask → assistant-answer exchanges (CHO-20 D10).

    Widget/tool_result turns are ``user`` messages whose content is a list of
    ``tool_result`` blocks — those do NOT count. Only plain-text user turns are
    substantive asks. Enforced by the harness, never by the model.
    """
    n = 0
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            n += 1
        elif isinstance(content, list) and not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            n += 1
    return n


class Agent:
    """The support agent. Owns the model client + tool implementations."""

    def __init__(
        self,
        client: Any,
        retriever: Any,
        finx_reports: Any = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._client = client
        self._retriever = retriever
        self._finx = finx_reports
        self._model = model or config.AGENT_MODEL
        self._max_tokens = max_tokens or config.MAX_TOKENS
        self._system_prompt = system_prompt or config.SYSTEM_PROMPT

    # -- request assembly --------------------------------------------------- #
    def _tools(self) -> list[dict]:
        tools = [SEARCH_TOOL]
        if self._finx is not None:
            from .finx.reports import CML_TOOL, CONTRACT_NOTE_TOOL
            tools += [CML_TOOL, CONTRACT_NOTE_TOOL]
        return tools

    def _system(self) -> list[dict]:
        # Frozen block with a prompt-cache breakpoint (CHO-20 D1). No per-request
        # data is interpolated, so the cache prefix stays stable.
        return [{
            "type": "text",
            "text": self._system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]

    # -- the loop ----------------------------------------------------------- #
    async def run_turn(
        self, messages: list[dict]
    ) -> AsyncGenerator[Event, str | None]:
        """Process one user turn, yielding typed events until the turn ends.

        The consumer must drive with ``asend`` (see :func:`drive`): send ``None``
        for ordinary events; when a :class:`UIRequestEvent` is yielded, send the
        user-selected value string back in.
        """
        # Conversation cap (CHO-20 D10) — graceful wind-down, not a hard cut.
        if count_substantive_exchanges(messages) > config.CONVERSATION_CAP:
            async for ev in self._wind_down():
                yield ev
            return

        convo = list(messages)
        try:
            while True:
                final = None
                # Stream one model turn; emit token deltas as they arrive.
                async with self._client.messages.stream(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system(),
                    tools=self._tools(),
                    thinking={"type": "adaptive"},  # not deprecated budget_tokens
                    messages=convo,
                ) as stream:
                    async for text in stream.text_stream:
                        if text:
                            yield TokenEvent(text=text)
                    final = await stream.get_final_message()

                convo.append({"role": "assistant", "content": final.content})
                tool_uses = [b for b in final.content
                             if getattr(b, "type", None) == "tool_use"]
                if final.stop_reason != "tool_use" or not tool_uses:
                    yield DoneEvent(stop_reason=final.stop_reason or "end_turn")
                    return

                # Execute each requested tool, appending results, streaming steps.
                results: list[dict] = []
                for block in tool_uses:
                    async for ev, result_block in self._run_tool(block):
                        if result_block is not None:
                            results.append(result_block)
                            continue
                        if not isinstance(ev, UIRequestEvent):
                            yield ev            # ordinary stream event (token/step)
                            continue
                        # A widget is required. Yield it and await the value the
                        # consumer sends back with asend() (CHO-20 D9).
                        sent = yield ev
                        if not sent:
                            # No selection this round-trip: the turn PAUSES. We emit
                            # no tool_result and stop — the client re-submits with the
                            # value next request. Widgets don't count toward the cap.
                            yield DoneEvent(stop_reason="awaiting_input")
                            return
                        # Re-run the tool with the injected value (date_picker).
                        async for ev2, rb2 in self._run_tool(
                            block, injected={"contract_date": sent}
                        ):
                            if rb2 is not None:
                                results.append(rb2)
                            else:
                                yield ev2
                convo.append({"role": "user", "content": results})
        except Exception as exc:  # noqa: BLE001 — surface as a terminal error event
            yield ErrorEvent(message=_safe_error(exc))

    async def _run_tool(
        self, block: Any, injected: dict | None = None
    ) -> AsyncGenerator[tuple[Event, dict | None], None]:
        """Run one tool call.

        Yields ``(event, result_block)`` pairs. A non-None ``result_block`` is a
        ``tool_result`` to append to the conversation; a None result_block means
        the yielded event is a UIRequestEvent the consumer must satisfy first.
        """
        name = block.name
        args = dict(block.input or {})
        if injected:
            args.update(injected)
        tuid = block.id

        if name == "search_knowledge_base":
            query = args.get("query", "")
            top_k = args.get("top_k")
            yield ToolUseEvent(
                tool=name,
                label=f"Searching the knowledge base for “{query}”",
                input=args,
            ), None
            chunks = await self._retriever.retrieve(query, top_k)
            yield ToolResultEvent(
                tool=name,
                summary=f"Found {len(chunks)} relevant article(s)",
            ), None
            if chunks:
                yield CitationsEvent(
                    citations=[{"id": c["id"], "topic": c["topic"]} for c in chunks]
                ), None
            yield (None, _tool_result(tuid, _chunks_for_model(chunks)))
            return

        if name == "get_cml_report":
            async for pair in self._report_tool(name, tuid, args, "cml"):
                yield pair
            return

        if name == "get_contract_note":
            # Model 2: the model triggers the tool but never supplies the date.
            if "contract_date" not in args:
                yield UIRequestEvent(
                    widget="date_picker",
                    spec={"format": "dd-mm-yyyy", "max": "today"},
                    correlation_id=tuid,
                    prompt="Which trading day's contract note do you need?",
                ), None
                return  # consumer will re-run us via injected=
            async for pair in self._report_tool(name, tuid, args, "contract_note"):
                yield pair
            return

        # Unknown tool — return an error result so the model can recover.
        yield (None, _tool_result(tuid, f"Unknown tool: {name}", is_error=True))

    async def _report_tool(
        self, name: str, tuid: str, args: dict, report_type: str
    ) -> AsyncGenerator[tuple[Event, dict | None], None]:
        from .finx.reports import ReportToolError

        mobile = args.get("mobile", "")
        label = "CML report" if report_type == "cml" else "contract note"
        yield ToolUseEvent(
            tool=name,
            label=f"Generating the {label} for {_mask_mobile(mobile)}…",
            input={k: v for k, v in args.items() if k != "mobile"} | {"mobile": _mask_mobile(mobile)},
        ), None
        try:
            if report_type == "cml":
                report: ReportEvent = await self._finx.get_cml_report(mobile)
            else:
                report = await self._finx.get_contract_note(mobile, args["contract_date"])
        except ReportToolError as exc:
            yield ToolResultEvent(tool=name, summary=str(exc)), None
            yield (None, _tool_result(tuid, str(exc), is_error=True))
            return
        yield ToolResultEvent(tool=name, summary=report.summary), None
        yield report, None  # the artifact/report SSE event (CHO-21 D4)
        yield (None, _tool_result(
            tuid, f"The {label} was generated and delivered to the user as an attachment."))

    async def _wind_down(self) -> AsyncGenerator[Event, None]:
        msg = ("We've covered a lot in this chat. To make sure you get the best "
               "help from here, would you like me to connect you to a human "
               "from our support team?")
        for ch in msg:
            yield TokenEvent(text=ch)
        yield UIRequestEvent(
            widget="choice",
            spec={"options": ["Connect me to a human", "No, I'm done"]},
            correlation_id="escalation",
            prompt="Connect to a human?",
        )
        yield DoneEvent(stop_reason="conversation_cap", escalated=True)


# --------------------------------------------------------------------------- #
# Consumer helper — bridges the send-protocol for callers that resolve widgets
# synchronously (tests, a CLI, an in-process client). The SSE server implements
# the same contract across the stateless HTTP boundary via the correlation id.
# --------------------------------------------------------------------------- #
async def drive(agent: Agent, messages: list[dict], widget_resolver=None):
    """Yield every event, satisfying UIRequests via ``widget_resolver``.

    ``widget_resolver(UIRequestEvent) -> str`` (sync or async) returns the value.
    If omitted, UIRequests yield through unanswered (the tool that needs the value
    will not proceed) — used when the transport handles the round-trip itself.
    """
    agen = agent.run_turn(messages)
    to_send: str | None = None
    while True:
        try:
            event = await agen.asend(to_send)
        except StopAsyncIteration:
            return
        to_send = None
        if isinstance(event, UIRequestEvent) and widget_resolver is not None:
            val = widget_resolver(event)
            if hasattr(val, "__await__"):
                val = await val
            to_send = val
        yield event


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


def _chunks_for_model(chunks: list[dict]) -> str:
    if not chunks:
        return ("No relevant knowledge-base articles were found. Tell the user you "
                "don't have that information rather than inventing an answer.")
    lines = []
    for c in chunks:
        lines.append(f"[id={c['id']} topic={c['topic']}]\n{c['chunk']}")
    return "\n\n".join(lines)


def _mask_mobile(mobile: str) -> str:
    digits = "".join(ch for ch in str(mobile) if ch.isdigit())
    if len(digits) >= 4:
        return "••••••" + digits[-4:]
    return "•" * len(digits)


def _safe_error(exc: Exception) -> str:
    # Client-safe: never leak secrets, tokens, or stack detail.
    return "Sorry — something went wrong while handling your request. Please try again."
