"""FastAPI Server-Sent-Events service (CHO-20 ``agent-sse-api``).

``POST /chat`` streams the agent's typed events as SSE; ``GET /health`` is
liveness. The API is stateless (CHO-20 D7): the client holds the conversation and
sends prior ``messages`` each request.

Widget round-trips across the stateless boundary (CHO-20 D9): when the agent needs
a ``date_picker``/``choice`` value, the stream emits a ``ui_request`` event with a
``correlation_id`` and the turn pauses. The client resubmits the same
conversation plus ``widget_values: {<correlation_id>: <selected value>}``; the
server injects it verbatim so the agent resumes using it. Widget round-trips do
not count toward the conversation cap.

Heavy clients (Anthropic, asyncpg pool, FinX auth) are built on startup and closed
on shutdown. FinX report tools are only wired when ``FINX_API_KEY`` is configured.
"""
from __future__ import annotations

from typing import Any

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import Agent, drive
from .events import ErrorEvent
from .retrieval import Retriever

app = FastAPI(title="Choice FinX support agent", version="0.1.0")

_STATIC = Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC), html=True), name="static")


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    # Correlation-id → selected value, for resolving a pending widget in-request.
    widget_values: dict[str, str] = {}
    # Optional per-request FinX bearer JWT copied from the logged-in browser. When
    # supplied it takes precedence over the env credential and scopes report calls
    # to THIS user's session identity (they can only fetch their own reports).
    finx_jwt: str | None = None


@app.on_event("startup")
async def _startup() -> None:
    config.load_env()
    import httpx
    from anthropic import AsyncAnthropic

    app.state.anthropic = AsyncAnthropic()
    app.state.retriever = await Retriever.create()
    # Shared HTTP client for FinX MIS calls (per-request FinxAuth reuses it).
    app.state.http = httpx.AsyncClient(timeout=config.FINX_HTTP_TIMEOUT_S)

    app.state.finx = None  # env-configured default (used when no per-request JWT)
    if config.finx_enabled():
        from .finx.auth import FinxAuth
        from .finx.reports import FinxReports

        app.state.finx = FinxReports(FinxAuth(client=app.state.http))


@app.on_event("shutdown")
async def _shutdown() -> None:
    ret = getattr(app.state, "retriever", None)
    if ret is not None:
        await ret.close()
    http = getattr(app.state, "http", None)
    if http is not None:
        await http.aclose()
    ac = getattr(app.state, "anthropic", None)
    if ac is not None:
        await ac.close()


def _finx_for_request(req: "ChatRequest"):
    """Per-request FinX tools: a browser JWT if supplied, else the env default."""
    if req.finx_jwt:
        from .finx.auth import FinxAuth
        from .finx.reports import FinxReports

        return FinxReports(FinxAuth(api_key=req.finx_jwt, client=app.state.http,
                                    mode="direct"))
    return getattr(app.state, "finx", None)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": config.AGENT_MODEL,
            "finx": bool(getattr(app.state, "finx", None))}


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    agent = Agent(
        client=app.state.anthropic,
        retriever=app.state.retriever,
        finx_reports=_finx_for_request(req),
    )

    def resolver(ev) -> str | None:
        # Return the client-supplied value for this pending widget, if any.
        return req.widget_values.get(ev.correlation_id)

    async def event_stream():
        try:
            async for event in drive(agent, req.messages, widget_resolver=resolver):
                yield event.to_sse()
        except Exception:  # noqa: BLE001 — never leak a stack to the client
            yield ErrorEvent(
                message="Sorry — the service hit an unexpected error."
            ).to_sse()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
