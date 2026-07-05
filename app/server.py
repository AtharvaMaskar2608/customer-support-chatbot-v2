"""FastAPI SSE service for the Choice FinX support agent (CHO-20).

Thin web layer over the reusable core in :mod:`app.agent`. It serialises the agent's
typed event stream to Server-Sent Events so the frontend sees the answer AND the
intermediate steps live:

    event: token        data: {"text": "..."}
    event: tool_use     data: {"tool": "...", "label": "Searching ...", "input": {...}}
    event: tool_result  data: {"tool": "...", "summary": "Found 5 ...", "count": 5}
    event: ui_request   data: {"widget": "choice", "label": "...", "correlation_id": "...",
                               "options": [...], "purpose": "...", "resume_messages": [...]}
    event: citations    data: {"citations": [{"chunk_id": 12, ...}]}
    event: done         data: {"state": {"exchanges": 1, "followups": 0}, "paused": false}
    event: error        data: {"message": "..."}

The API is stateless (design D7): the client POSTs the whole conversation plus the
small ``state`` counters, and gets the updated state back on the terminal event.

Run:
    uvicorn app.server:app --reload
Then open http://127.0.0.1:8000/ for the built-in test client.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from anthropic import AsyncAnthropic
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config
from .agent import SupportAgent
from .events import AgentEvent
from .retrieval import Retriever

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load_env()
    dsn, _ = config.require_env()
    retriever = await Retriever.create(dsn)
    anthropic = AsyncAnthropic(timeout=config.ANTHROPIC_TIMEOUT_S)
    _state["retriever"] = retriever
    _state["agent"] = SupportAgent(anthropic, retriever)
    _state["anthropic"] = anthropic
    try:
        yield
    finally:
        await retriever.close()
        await anthropic.close()


app = FastAPI(title="Choice FinX Support Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    """Stateless chat request: the full conversation + the harness counters."""

    messages: list[dict] = Field(..., min_length=1)
    state: dict | None = None


def _sse(event: AgentEvent) -> str:
    payload = {"type": event.type, **event.data()}
    return f"event: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/chat")
async def chat(req: ChatRequest):
    """Stream one agent turn as SSE over the posted conversation."""
    agent: SupportAgent = _state["agent"]

    async def gen():
        async for event in agent.run(req.messages, req.state):
            yield _sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {"ok": True, "model": config.AGENT_MODEL}


@app.get("/", response_class=HTMLResponse)
async def index():
    return _TEST_CLIENT_HTML


# --------------------------------------------------------------------------- #
# Thin static test client — deliberately minimal (not the production UI, which is
# out of scope for CHO-20). Uses fetch()+ReadableStream to POST and parse SSE,
# renders intermediate steps live, handles ui_request widgets, and keeps the
# stateless conversation + counters client-side across turns.
# --------------------------------------------------------------------------- #
_TEST_CLIENT_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Choice FinX Support Agent</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; }
  #log { border: 1px solid #8884; border-radius: 8px; padding: 1rem; min-height: 240px; }
  .msg { white-space: pre-wrap; margin: .25rem 0; }
  .user { font-weight: 600; }
  .answer { }
  .step { color: #888; font-style: italic; }
  .cite { color: #888; font-size: .85em; margin: .35rem 0 .75rem; }
  .err { color: #c33; }
  .widget { margin: .5rem 0; padding: .6rem; border: 1px dashed #8886; border-radius: 6px; }
  .row { display: flex; gap: .5rem; margin-top: 1rem; }
  input { flex: 1; padding: .6rem .8rem; border-radius: 6px; border: 1px solid #8888; font: inherit; }
  button { padding: .5rem 1rem; border-radius: 6px; border: 0; background: #2b6; color: #fff; font: inherit; cursor: pointer; }
  button.opt { background: #47c; margin: .2rem .3rem .2rem 0; }
  button:disabled { opacity: .5; cursor: default; }
</style>
</head>
<body>
<h1>Choice FinX — Support Agent</h1>
<p class="step">Ask a product question. Watch the agent search the KB live; it may ask a follow-up or pop a widget.</p>
<div id="log"></div>
<div class="row">
  <input id="q" placeholder="e.g. Is brokerage charged on square-off trades?" autofocus>
  <button id="send">Send</button>
</div>
<script>
const log = document.getElementById('log');
const input = document.getElementById('q');
const btn = document.getElementById('send');

let history = [];                       // Anthropic messages (client-held, stateless)
let convState = { exchanges: 0, followups: 0 };

function el(cls, text) {
  const d = document.createElement('div');
  if (cls) d.className = cls;
  if (text != null) d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
function setBusy(b) { btn.disabled = b; input.disabled = b; }

async function send(body) {
  setBusy(true);
  const answer = el('msg answer', '');
  let answerText = '';
  let paused = false;
  const resp = await fetch('/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\\n\\n')) >= 0) {
      const frame = buf.slice(0, i); buf = buf.slice(i + 2);
      const ev = {};
      for (const line of frame.split('\\n')) {
        if (line.startsWith('event:')) ev.event = line.slice(6).trim();
        else if (line.startsWith('data:')) ev.data = line.slice(5).trim();
      }
      if (!ev.data) continue;
      const d = JSON.parse(ev.data);
      if (ev.event === 'token') { answerText += d.text; answer.textContent = answerText; }
      else if (ev.event === 'tool_use') el('msg step', '🔎 ' + d.label);
      else if (ev.event === 'tool_result') el('msg step', '✓ ' + d.summary);
      else if (ev.event === 'citations') {
        if (d.citations.length) el('cite', 'Sources: ' + d.citations.map(c => 'KB #' + c.chunk_id + ' (' + c.topic + ')').join(', '));
      }
      else if (ev.event === 'ui_request') { paused = true; renderWidget(d); }
      else if (ev.event === 'done') {
        convState = d.state || convState;
        if (!d.paused && answerText) history.push({ role: 'assistant', content: answerText });
      }
      else if (ev.event === 'error') el('msg err', 'Error: ' + d.message);
    }
  }
  setBusy(!paused ? false : true);   // stay disabled until the widget is answered
  if (!paused) input.focus();
}

function renderWidget(d) {
  const box = el('widget');
  el('msg', d.label);   // question / prompt
  const resume = (value) => {
    box.remove();
    if (d.purpose === 'escalation') {
      // Harness offer — no pending tool call; answer as a plain new user turn.
      history.push({ role: 'user', content: value });
      el('msg user', 'you › ' + value);
    } else {
      // Widget/clarification — resume the pending tool call with the value.
      history = d.resume_messages;
      history.push({ role: 'user', content: [
        { type: 'tool_result', tool_use_id: d.correlation_id, content: value },
      ]});
      el('msg user', 'you › ' + value);
    }
    send({ messages: history, state: convState });
  };
  if (d.widget === 'choice' || d.purpose === 'escalation') {
    for (const opt of (d.options || [])) {
      const b = document.createElement('button'); b.className = 'opt'; b.textContent = opt;
      b.onclick = () => resume(opt); box.appendChild(b);
    }
  } else if (d.widget === 'date_picker') {
    const inp = document.createElement('input'); inp.type = 'date';
    const b = document.createElement('button'); b.textContent = 'OK';
    b.onclick = () => inp.value && resume(inp.value);
    box.appendChild(inp); box.appendChild(b);
  } else {  // free-text clarification
    const inp = document.createElement('input'); inp.placeholder = 'Type your answer…';
    const b = document.createElement('button'); b.textContent = 'Send';
    const go = () => inp.value.trim() && resume(inp.value.trim());
    b.onclick = go; inp.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
    box.appendChild(inp); box.appendChild(b); inp.focus();
  }
}

function ask() {
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  el('msg user', 'you › ' + message);
  history.push({ role: 'user', content: message });
  send({ messages: history, state: convState });
}

btn.onclick = ask;
input.addEventListener('keydown', e => { if (e.key === 'Enter') ask(); });
</script>
</body>
</html>
"""
