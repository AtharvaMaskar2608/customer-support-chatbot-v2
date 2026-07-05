"""Typed agent event stream (CHO-20).

The agent core yields a stream of these events; the web layer serialises them to SSE.
Keeping the event model generic (a ``tool_use`` carries a human-readable label, not a
retrieval-specific shape) means a future ``query_account_api`` tool surfaces the same
way with no change to the transport.

Event kinds:
  * ``token``        — a chunk of the assistant's answer, streamed token-by-token
  * ``tool_use``     — emitted BEFORE a tool runs, with a human-readable label
  * ``tool_result``  — a short summary of what the tool returned
  * ``ui_request``   — the agent asks the user for structured input (a widget); the
                       turn PAUSES until the client returns a selection. Carries a
                       correlation id and the conversation to resume from. The risky
                       value is collected from the widget, never from LLM free text.
  * ``citations``    — the kb_faq chunk ids the answer is grounded in
  * ``done``         — the turn finished (or paused for input); carries the updated
                       conversation ``state`` (exchange / follow-up counters)
  * ``error``        — the turn aborted; ``message`` explains why
"""
from __future__ import annotations

from dataclasses import dataclass, field


class AgentEvent:
    """Base class — every event exposes ``type`` and a JSON-serialisable ``data``."""

    type: str = "event"

    def data(self) -> dict:  # pragma: no cover - overridden
        return {}


@dataclass
class TokenEvent(AgentEvent):
    text: str
    type: str = field(default="token", init=False)

    def data(self) -> dict:
        return {"text": self.text}


@dataclass
class ToolUseEvent(AgentEvent):
    """Emitted before a tool call runs so the UI can show live progress."""

    tool: str
    label: str
    tool_input: dict
    type: str = field(default="tool_use", init=False)

    def data(self) -> dict:
        return {"tool": self.tool, "label": self.label, "input": self.tool_input}


@dataclass
class ToolResultEvent(AgentEvent):
    tool: str
    summary: str
    count: int
    type: str = field(default="tool_result", init=False)

    def data(self) -> dict:
        return {"tool": self.tool, "summary": self.summary, "count": self.count}


@dataclass
class UiRequestEvent(AgentEvent):
    """Ask the user for a structured value through a rendered widget.

    ``widget`` is one of ``date_picker`` | ``choice`` (extensible). ``options`` is
    used by ``choice``. ``correlation_id`` ties the eventual selection back to the
    pending request. ``resume_messages`` is the conversation the client must POST
    back (with the selection appended as a tool_result carrying ``correlation_id``)
    to continue the turn — the API is stateless, so the pending assistant turn rides
    on the event rather than being held server-side. ``purpose`` distinguishes a
    plain value request from a clarifying follow-up or an escalation Yes/No.
    """

    widget: str
    label: str
    correlation_id: str
    options: list[str] | None = None
    purpose: str = "input"  # "input" | "clarification" | "escalation"
    resume_messages: list | None = None
    type: str = field(default="ui_request", init=False)

    def data(self) -> dict:
        return {
            "widget": self.widget,
            "label": self.label,
            "correlation_id": self.correlation_id,
            "options": self.options,
            "purpose": self.purpose,
            "resume_messages": self.resume_messages,
        }


@dataclass
class CitationsEvent(AgentEvent):
    """The kb_faq articles the answer draws on. ``citations`` is a list of
    ``{chunk_id, topic, question}`` dicts, de-duplicated in retrieval order."""

    citations: list[dict]
    type: str = field(default="citations", init=False)

    def data(self) -> dict:
        return {"citations": self.citations}


@dataclass
class DoneEvent(AgentEvent):
    """Terminal event. ``state`` is the updated conversation state (exchange /
    follow-up counters) the stateless client passes back on its next request.
    ``paused`` is True when the turn ended awaiting widget input rather than
    completing an answer."""

    state: dict
    paused: bool = False
    type: str = field(default="done", init=False)

    def data(self) -> dict:
        return {"state": self.state, "paused": self.paused}


@dataclass
class ErrorEvent(AgentEvent):
    message: str
    type: str = field(default="error", init=False)

    def data(self) -> dict:
        return {"message": self.message}
