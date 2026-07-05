"""Typed SSE event model — the core contract between the agent and any transport.

The agent core (``app.agent``) yields these events; the FastAPI layer
(``app.server``) serializes them to Server-Sent Events with :meth:`Event.to_sse`.
Because the core yields typed events rather than the server reaching into the
loop, the same core can drive a CLI or an eval harness (CHO-20 D4 / "reusable
core").

Event types
-----------
CHO-20 (``support-agent`` / ``agent-sse-api``):
  * ``token``       — an assistant text delta.
  * ``tool_use``    — an intermediate step announced BEFORE a tool runs, with a
                      human-readable label + the structured input.
  * ``tool_result`` — a short summary once a tool finishes ("Found 5 articles").
  * ``citations``   — the ``kb_faq`` ids/topics the answer drew on.
  * ``ui_request``  — a widget spec (``date_picker`` / ``choice``) + correlation
                      id; the turn pauses until the client returns a value. The
                      risky field is absent from the LLM tool schema (Model 2).
  * ``done``        — terminal success.
  * ``error``       — terminal failure, carries a message.

CHO-21 (``account-report-tools``):
  * ``report``      — the artifact event carrying a generated report (a link,
                      payload, or summary) so the client can present it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class Event:
    """Base SSE event. Subclasses set ``type`` and implement :meth:`data`."""

    type: str = "event"

    def data(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def to_sse(self) -> str:
        """Serialize to an SSE frame: ``event: <type>\\ndata: <json>\\n\\n``."""
        payload = json.dumps(self.data(), ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.type}\ndata: {payload}\n\n"


@dataclass
class TokenEvent(Event):
    """An incremental slice of the assistant's answer."""
    text: str
    type: str = field(default="token", init=False)

    def data(self) -> dict[str, Any]:
        return {"text": self.text}


@dataclass
class ToolUseEvent(Event):
    """A tool call, announced before it runs (CHO-20 D5 — human-readable step)."""
    tool: str
    label: str            # e.g. "Searching the knowledge base for 'square-off'"
    input: dict[str, Any]  # structured input, for debugging/rendering
    type: str = field(default="tool_use", init=False)

    def data(self) -> dict[str, Any]:
        return {"tool": self.tool, "label": self.label, "input": self.input}


@dataclass
class ToolResultEvent(Event):
    """A short, user-facing summary of a completed tool call."""
    tool: str
    summary: str          # e.g. "Found 5 relevant articles"
    type: str = field(default="tool_result", init=False)

    def data(self) -> dict[str, Any]:
        return {"tool": self.tool, "summary": self.summary}


@dataclass
class CitationsEvent(Event):
    """The kb_faq chunks the answer relied on, so the client can show sources."""
    citations: list[dict[str, Any]]  # [{id, topic}]
    type: str = field(default="citations", init=False)

    def data(self) -> dict[str, Any]:
        return {"citations": self.citations}


@dataclass
class UIRequestEvent(Event):
    """Ask the client for structured input via a widget (CHO-20 D9, Model 2).

    ``widget`` is e.g. ``date_picker`` or ``choice``. ``spec`` carries the
    widget's parameters (format, ``max``, option list). ``correlation_id`` is
    echoed back by the client with the selection so the paused intent resumes.
    """
    widget: str
    spec: dict[str, Any]
    correlation_id: str
    prompt: str = ""
    type: str = field(default="ui_request", init=False)

    def data(self) -> dict[str, Any]:
        return {
            "widget": self.widget,
            "spec": self.spec,
            "correlation_id": self.correlation_id,
            "prompt": self.prompt,
        }


@dataclass
class ReportEvent(Event):
    """A generated FinX report, delivered as a typed artifact (CHO-21 D4).

    Shape-agnostic so both CML and contract note use it: whichever the MIS API
    returns — a download ``url``, base64 ``content`` (+ ``mime``/``filename``),
    or structured ``payload`` — travels here alongside a short ``summary``.
    """
    report_type: str                 # "cml" | "contract_note"
    summary: str
    url: str | None = None
    filename: str | None = None
    mime: str | None = None
    content_b64: str | None = None
    payload: dict[str, Any] | None = None
    type: str = field(default="report", init=False)

    def data(self) -> dict[str, Any]:
        d: dict[str, Any] = {"report_type": self.report_type, "summary": self.summary}
        for k in ("url", "filename", "mime", "content_b64", "payload"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


@dataclass
class DoneEvent(Event):
    """Terminal success. ``stop_reason`` mirrors the model's final stop."""
    stop_reason: str = "end_turn"
    escalated: bool = False
    type: str = field(default="done", init=False)

    def data(self) -> dict[str, Any]:
        return {"stop_reason": self.stop_reason, "escalated": self.escalated}


@dataclass
class ErrorEvent(Event):
    """Terminal failure carrying a client-safe message (never a secret/stack)."""
    message: str
    type: str = field(default="error", init=False)

    def data(self) -> dict[str, Any]:
        return {"message": self.message}
