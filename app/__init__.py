"""Choice FinX customer-support agent (application core).

The first product code in the repo, sibling to ``evals/``. It puts the RAG
substrate validated in CHO-16→19 behind a streaming Claude tool-use agent
(CHO-20, ``support-agent``) and layers the read-only FinX account-report tools
on top (CHO-21, ``finx-report-tools``).

Sub-packages:
  * ``app.config``    — env config, model ids, the frozen system prompt.
  * ``app.events``    — the typed SSE event model (the core contract).
  * ``app.retrieval`` — read-only hybrid-RRF retrieval over ``kb_faq``.
  * ``app.agent``     — the streaming agentic loop + tool registry + caps.
  * ``app.server``    — the FastAPI SSE service.
  * ``app.finx``      — M2M JWT auth + the two read-only report tools.
"""
