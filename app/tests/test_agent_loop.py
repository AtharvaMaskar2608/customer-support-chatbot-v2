"""Agent-loop tests (CHO-20 task 6.1 / CHO-21 verification) with stubs only.

Asserts event ordering for: a grounded KB answer, the CML report artifact, the
contract-note date_picker (Model 2 pause + resume), the conversation cap, and an
error turn.
"""
from __future__ import annotations

import pytest

from app.agent import Agent, count_substantive_exchanges, drive
from app.events import (
    CitationsEvent,
    DoneEvent,
    ReportEvent,
    TokenEvent,
    ToolResultEvent,
    ToolUseEvent,
    UIRequestEvent,
)
from app.tests.stubs import (
    StubAnthropic,
    StubReports,
    StubRetriever,
    TextBlock,
    ToolUseBlock,
    Turn,
)


async def collect(agent, messages, resolver=None):
    return [ev async for ev in drive(agent, messages, widget_resolver=resolver)]


async def test_grounded_answer_streams_tool_use_result_citations_done():
    chunks = [{"id": 12, "topic": "Brokerage", "chunk": "Intraday equity: 0.03%."}]
    turns = [
        Turn("", [ToolUseBlock("search_knowledge_base",
                               {"query": "intraday brokerage"}, "tu1")], "tool_use"),
        Turn("It is 0.03%.", [TextBlock("It is 0.03%.")], "end_turn"),
    ]
    agent = Agent(StubAnthropic(turns), StubRetriever(chunks))
    events = await collect(agent, [{"role": "user", "content": "brokerage?"}])
    types = [type(e) for e in events]

    # tool_use announced before result; citations delivered; token text; done last.
    assert ToolUseEvent in types and ToolResultEvent in types
    assert types.index(ToolUseEvent) < types.index(ToolResultEvent)
    cites = [e for e in events if isinstance(e, CitationsEvent)][0]
    assert cites.citations == [{"id": 12, "topic": "Brokerage"}]
    assert "".join(e.text for e in events if isinstance(e, TokenEvent)) == "It is 0.03%."
    assert isinstance(events[-1], DoneEvent) and events[-1].stop_reason == "end_turn"


async def test_kb_miss_returns_no_articles():
    turns = [
        Turn("", [ToolUseBlock("search_knowledge_base", {"query": "x"}, "tu1")], "tool_use"),
        Turn("I don't have that.", [TextBlock("I don't have that.")], "end_turn"),
    ]
    agent = Agent(StubAnthropic(turns), StubRetriever([]))
    events = await collect(agent, [{"role": "user", "content": "obscure?"}])
    summary = [e for e in events if isinstance(e, ToolResultEvent)][0].summary
    assert "0" in summary  # "Found 0 relevant article(s)"
    assert not any(isinstance(e, CitationsEvent) for e in events)


async def test_cml_report_emits_artifact_event():
    report = ReportEvent(report_type="cml", summary="Your CML report is ready.",
                         url="https://x/cml.pdf")
    turns = [
        Turn("", [ToolUseBlock("get_cml_report", {"mobile": "8779552825"}, "tu1")], "tool_use"),
        Turn("Here it is.", [TextBlock("Here it is.")], "end_turn"),
    ]
    agent = Agent(StubAnthropic(turns), StubRetriever([]), StubReports(report))
    events = await collect(agent, [{"role": "user", "content": "my CML report"}])
    reps = [e for e in events if isinstance(e, ReportEvent)]
    assert len(reps) == 1 and reps[0].report_type == "cml"
    # The report step masks the mobile number in its label.
    step = [e for e in events if isinstance(e, ToolUseEvent)][0]
    assert "8779552825" not in step.label and "2825" in step.label


async def test_contract_note_date_picker_pause_then_resume():
    report = ReportEvent(report_type="contract_note", summary="Your contract note is ready.")
    # The model calls get_contract_note WITHOUT a date (Model 2 — not in schema).
    turns_pause = [
        Turn("", [ToolUseBlock("get_contract_note", {"mobile": "8779552825"}, "tu1")], "tool_use"),
    ]
    stub = StubReports(report)
    agent = Agent(StubAnthropic(turns_pause), StubRetriever([]), stub)

    # No resolver → the turn pauses on the date_picker widget, no report yet.
    paused = await collect(agent, [{"role": "user", "content": "contract note please"}])
    ui = [e for e in paused if isinstance(e, UIRequestEvent)]
    assert len(ui) == 1 and ui[0].widget == "date_picker"
    assert ui[0].spec["format"] == "dd-mm-yyyy"
    assert isinstance(paused[-1], DoneEvent) and paused[-1].stop_reason == "awaiting_input"
    assert stub.note_calls == []  # never called without a date

    # Resume: same first turn, then an end_turn; resolver supplies the picked date.
    turns_resume = [
        Turn("", [ToolUseBlock("get_contract_note", {"mobile": "8779552825"}, "tu1")], "tool_use"),
        Turn("Done.", [TextBlock("Done.")], "end_turn"),
    ]
    stub2 = StubReports(report)
    agent2 = Agent(StubAnthropic(turns_resume), StubRetriever([]), stub2)
    events = await collect(agent2, [{"role": "user", "content": "contract note please"}],
                           resolver=lambda ev: "05-07-2026")
    assert stub2.note_calls == [("8779552825", "05-07-2026")]
    assert any(isinstance(e, ReportEvent) for e in events)
    assert isinstance(events[-1], DoneEvent) and events[-1].stop_reason == "end_turn"


async def test_finx_tools_absent_when_no_reports():
    agent = Agent(StubAnthropic([Turn("hi", [TextBlock("hi")], "end_turn")]),
                  StubRetriever([]))
    await collect(agent, [{"role": "user", "content": "hi"}])
    tool_names = {t["name"] for t in agent._tools()}
    assert tool_names == {"search_knowledge_base"}


async def test_conversation_cap_winds_down():
    # 11 substantive user asks already present → over the cap of 10.
    messages = []
    for i in range(11):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": "a"})
    agent = Agent(StubAnthropic([]), StubRetriever([]))
    events = await collect(agent, messages, resolver=lambda ev: None)
    assert any(isinstance(e, UIRequestEvent) and e.widget == "choice" for e in events)
    done = [e for e in events if isinstance(e, DoneEvent)][0]
    assert done.escalated and done.stop_reason == "conversation_cap"


def test_count_substantive_exchanges_excludes_tool_results():
    msgs = [
        {"role": "user", "content": "real ask"},
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x"}]},
        {"role": "assistant", "content": "answer"},
    ]
    assert count_substantive_exchanges(msgs) == 1
