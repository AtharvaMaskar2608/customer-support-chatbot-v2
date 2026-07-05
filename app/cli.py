"""Minimal CLI driver for the support agent core (CHO-20).

Proves the same core (:mod:`app.agent`) drives a terminal client, not just the SSE
service — and is the quickest way to smoke-test end-to-end against the real kb_faq.
Handles widget/clarification pauses by prompting on stdin and resuming.

    python -m app.cli "Is brokerage charged on square-off trades?"
    python -m app.cli            # interactive REPL
"""
from __future__ import annotations

import asyncio
import sys

from anthropic import AsyncAnthropic

from . import config
from .agent import SupportAgent
from .events import (
    CitationsEvent, DoneEvent, ErrorEvent, TokenEvent, ToolResultEvent,
    ToolUseEvent, UiRequestEvent,
)
from .retrieval import Retriever

DIM, RESET, RED = "\033[2m", "\033[0m", "\033[31m"


async def _one_pass(agent, messages, state):
    """Run a single agent turn; return (state, answer_text, ui_request or None)."""
    answer = ""
    ui = None
    async for event in agent.run(messages, state):
        if isinstance(event, TokenEvent):
            answer += event.text
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolUseEvent):
            print(f"\n  {DIM}🔎 {event.label}{RESET}", flush=True)
        elif isinstance(event, ToolResultEvent):
            print(f"  {DIM}✓ {event.summary}{RESET}\n", flush=True)
        elif isinstance(event, UiRequestEvent):
            ui = event
        elif isinstance(event, CitationsEvent):
            cites = ", ".join(f"KB #{c['chunk_id']} ({c['topic']})"
                              for c in event.citations)
            print(f"\n{DIM}Sources: {cites}{RESET}")
        elif isinstance(event, DoneEvent):
            state = event.state
            print()
        elif isinstance(event, ErrorEvent):
            print(f"\n{RED}Error: {event.message}{RESET}", file=sys.stderr)
    return state, answer, ui


async def converse(agent, history, state, message):
    """Handle one user ask fully, including any widget/clarification round-trips."""
    history.append({"role": "user", "content": message})
    while True:
        state, answer, ui = await _one_pass(agent, history, state)
        if ui is None:
            if answer:
                history.append({"role": "assistant", "content": answer})
            return history, state
        # Widget/clarification/escalation — prompt and resume.
        prompt = ui.label
        if ui.options:
            prompt += "  [" + " / ".join(ui.options) + "]"
        value = input(f"\n{DIM}› {prompt}{RESET}\n  your input: ").strip()
        if ui.purpose == "escalation":
            history.append({"role": "user", "content": value})
        else:
            history = ui.resume_messages
            history.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": ui.correlation_id,
                "content": value}]})


async def main_async(argv: list[str]) -> int:
    config.load_env()
    dsn, _ = config.require_env()
    retriever = await Retriever.create(dsn)
    anthropic = AsyncAnthropic(timeout=config.ANTHROPIC_TIMEOUT_S)
    agent = SupportAgent(anthropic, retriever)
    print(f"model: {config.AGENT_MODEL}  ·  kb_faq retrieval ready", file=sys.stderr)
    history: list[dict] = []
    state: dict = {"exchanges": 0, "followups": 0}
    try:
        if len(argv) > 1:
            await converse(agent, history, state, " ".join(argv[1:]))
        else:
            print("Interactive — type a question (Ctrl-D to quit).", file=sys.stderr)
            while True:
                try:
                    message = input("\nyou › ").strip()
                except EOFError:
                    break
                if message:
                    history, state = await converse(agent, history, state, message)
    finally:
        await retriever.close()
        await anthropic.close()
    return 0


def main() -> int:
    return asyncio.run(main_async(sys.argv))


if __name__ == "__main__":
    raise SystemExit(main())
