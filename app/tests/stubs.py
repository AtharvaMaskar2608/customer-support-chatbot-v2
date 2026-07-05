"""Test doubles: a scripted Anthropic streaming client and stub tools.

No network, no real SDK behaviour — just enough surface to drive the agent loop:
a ``messages.stream(...)`` async context manager whose ``text_stream`` yields the
scripted text and whose ``get_final_message()`` returns a scripted message with
``content`` blocks and a ``stop_reason``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# --- scripted model message blocks ---------------------------------------- #
@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict
    id: str
    type: str = "tool_use"


@dataclass
class FinalMessage:
    content: list
    stop_reason: str


@dataclass
class Turn:
    """One scripted model turn: some streamed text + a final message."""
    text: str
    content: list
    stop_reason: str


class _Stream:
    def __init__(self, turn: Turn) -> None:
        self._turn = turn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        turn = self._turn

        async def gen():
            for ch in turn.text:
                yield ch
        return gen()

    async def get_final_message(self):
        return FinalMessage(self._turn.content, self._turn.stop_reason)


class _Messages:
    def __init__(self, client: "StubAnthropic") -> None:
        self._client = client

    def stream(self, **kwargs):
        self._client.calls.append(kwargs)
        turn = self._client.turns[self._client.i]
        self._client.i += 1
        return _Stream(turn)


class StubAnthropic:
    """Replays ``turns`` in order for successive ``messages.stream`` calls."""

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = turns
        self.i = 0
        self.calls: list[dict] = []
        self.messages = _Messages(self)


class StubRetriever:
    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks
        self.queries: list[str] = []

    async def retrieve(self, query: str, top_k=None) -> list[dict]:
        self.queries.append(query)
        return list(self._chunks)


@dataclass
class StubReports:
    """Stub FinX report tools; records calls, returns a canned ReportEvent."""
    report: Any
    cml_calls: list = field(default_factory=list)
    note_calls: list = field(default_factory=list)

    async def get_cml_report(self, mobile: str):
        self.cml_calls.append(mobile)
        return self.report

    async def get_contract_note(self, mobile: str, contract_date: str):
        self.note_calls.append((mobile, contract_date))
        return self.report
