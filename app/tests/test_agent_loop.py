"""Unit tests for the agent loop with a stubbed model + stubbed tool (task 6.1).

No network, no DB — a scripted fake Anthropic stream drives the loop so we can assert
event ordering and the harness-enforced caps deterministically. Run either with
pytest, or standalone:

    python -m app.tests.test_agent_loop
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import config
from app.agent import SupportAgent
from app.events import (
    CitationsEvent, DoneEvent, TokenEvent, ToolResultEvent, ToolUseEvent,
    UiRequestEvent,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_block(name, tool_input, id="toolu_1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=tool_input)


def _delta(text):
    return SimpleNamespace(type="content_block_delta",
                           delta=SimpleNamespace(type="text_delta", text=text))


class _FakeStream:
    def __init__(self, turn):
        self._turn = turn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def __aiter__(self):
        for chunk in self._turn.get("text", ""):
            yield _delta(chunk)

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self._turn["stop_reason"],
                               content=self._turn["content"])


class _FakeMessages:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    def stream(self, **kwargs):
        self.calls += 1
        return _FakeStream(self._turns.pop(0))


class _FakeAnthropic:
    def __init__(self, turns):
        self.messages = _FakeMessages(turns)


class _FakeRetriever:
    def __init__(self, results):
        self._results = results
        self.searches = 0

    async def search(self, query, top_k=None):
        self.searches += 1
        return self._results


async def _collect(agent, messages, state=None):
    return [ev async for ev in agent.run(messages, state)]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_search_then_answer_ordering():
    turns = [
        {"stop_reason": "tool_use", "text": "",
         "content": [_tool_block("search_knowledge_base", {"query": "square off"})]},
        {"stop_reason": "end_turn", "text": ["Yes", ", brokerage applies."],
         "content": [_text_block("Yes, brokerage applies.")]},
    ]
    retr = _FakeRetriever([
        {"id": 967, "topic": "Charges", "question": "Q", "chunk": "A"},
    ])
    agent = SupportAgent(_FakeAnthropic(turns), retr)
    events = await _collect(agent, [{"role": "user", "content": "square off?"}])
    kinds = [e.type for e in events]

    assert kinds == ["tool_use", "tool_result", "token", "token", "citations", "done"], kinds
    assert retr.searches == 1
    tr = next(e for e in events if isinstance(e, ToolResultEvent))
    assert tr.count == 1 and "1 relevant article" in tr.summary
    cites = next(e for e in events if isinstance(e, CitationsEvent))
    assert cites.citations[0]["chunk_id"] == 967
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.state == {"exchanges": 1, "followups": 0} and done.paused is False
    print("PASS test_search_then_answer_ordering")


async def test_collect_input_pauses_free_of_caps():
    turns = [
        {"stop_reason": "tool_use", "text": "",
         "content": [_tool_block("collect_input",
                                  {"kind": "date_picker", "label": "Pick a date"})]},
    ]
    agent = SupportAgent(_FakeAnthropic(turns), _FakeRetriever([]))
    events = await _collect(agent, [{"role": "user", "content": "ledger for a day"}],
                            {"exchanges": 3, "followups": 0})
    ui = next(e for e in events if isinstance(e, UiRequestEvent))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert ui.widget == "date_picker" and ui.purpose == "input"
    assert ui.correlation_id == "toolu_1" and ui.resume_messages is not None
    assert done.paused is True
    assert done.state["exchanges"] == 3 and done.state["followups"] == 0  # widget is free
    print("PASS test_collect_input_pauses_free_of_caps")


async def test_clarification_within_cap_pauses_and_counts():
    turns = [
        {"stop_reason": "tool_use", "text": "",
         "content": [_tool_block("ask_clarification",
                                  {"question": "Which report?",
                                   "options": ["Contract note", "CML"]})]},
    ]
    agent = SupportAgent(_FakeAnthropic(turns), _FakeRetriever([]))
    events = await _collect(agent, [{"role": "user", "content": "my report"}],
                            {"exchanges": 0, "followups": 0})
    ui = next(e for e in events if isinstance(e, UiRequestEvent))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert ui.purpose == "clarification" and ui.widget == "choice"
    assert done.state["followups"] == 1 and done.paused is True   # counts toward the 2-cap
    print("PASS test_clarification_within_cap_pauses_and_counts")


async def test_followup_cap_offers_escalation():
    # followups already at the cap; the model asks again → escalate instead.
    turns = [
        {"stop_reason": "tool_use", "text": "",
         "content": [_tool_block("ask_clarification", {"question": "Which one?"})]},
        # _finish_with_escalation makes a second model call:
        {"stop_reason": "end_turn",
         "text": ["I'm not able to pin this down — connect you to a human?"],
         "content": [_text_block("connect you to a human?")]},
    ]
    agent = SupportAgent(_FakeAnthropic(turns), _FakeRetriever([]))
    events = await _collect(agent, [{"role": "user", "content": "still unclear"}],
                            {"exchanges": 1, "followups": config.MAX_FOLLOWUPS})
    ui = [e for e in events if isinstance(e, UiRequestEvent)]
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert len(ui) == 1 and ui[0].purpose == "escalation"
    assert done.state["followups"] == 0        # request closed → reset
    print("PASS test_followup_cap_offers_escalation")


async def test_conversation_cap_winds_down_without_model():
    fake = _FakeAnthropic([])   # no scripted turns — model must NOT be called
    retr = _FakeRetriever([])
    agent = SupportAgent(fake, retr)
    events = await _collect(agent, [{"role": "user", "content": "one more thing"}],
                            {"exchanges": config.MAX_EXCHANGES, "followups": 0})
    kinds = [e.type for e in events]
    ui = next(e for e in events if isinstance(e, UiRequestEvent))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert fake.messages.calls == 0 and retr.searches == 0   # harness-enforced
    assert "token" in kinds and ui.purpose == "escalation" and done.paused is True
    print("PASS test_conversation_cap_winds_down_without_model")


_TESTS = [
    test_search_then_answer_ordering,
    test_collect_input_pauses_free_of_caps,
    test_clarification_within_cap_pauses_and_counts,
    test_followup_cap_offers_escalation,
    test_conversation_cap_winds_down_without_model,
]


async def _run_all():
    for t in _TESTS:
        await t()
    print(f"\nAll {len(_TESTS)} agent-loop tests passed.")


if __name__ == "__main__":
    asyncio.run(_run_all())
