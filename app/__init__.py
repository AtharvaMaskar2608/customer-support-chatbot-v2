"""Choice FinX customer-support agent (CHO-20).

The first real application on the RAG substrate validated in CHO-16 → CHO-19:
a Claude tool-use agent that answers brokerage/trading/demat questions grounded
in ``kb_faq`` via a hybrid-RRF ``search_knowledge_base`` tool, and streams both
its answer and its intermediate steps.

Layered as a reusable core (``config`` / ``retrieval`` / ``events`` / ``agent``)
with a thin FastAPI SSE layer (``server``) on top, so the same core can also drive
a CLI or an eval harness.
"""
