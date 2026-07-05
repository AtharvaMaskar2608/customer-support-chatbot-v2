"""Hybrid-RRF knowledge retrieval + the ``search_knowledge_base`` Claude tool (CHO-20).

This is the single runtime source of truth for the retrieval recipe validated in
CHO-17 (dense vector + full-text + Reciprocal Rank Fusion, all in Postgres) and
CHO-19 (~97% recall@10). The RRF SQL and the ``text-embedding-3-large`` @1536 query
embedding are lifted from the CHO-17 benchmark; here they are wrapped as a reusable
retriever plus the Anthropic tool schema the agent calls. Read-only against ``kb_faq``.
"""
from __future__ import annotations

import asyncpg
from openai import AsyncOpenAI

from . import config


# --------------------------------------------------------------------------- #
# SQL — the two arms fused in a single round-trip, joined back to content.
# Same RRF recipe as CHO-17 (rank each arm, FULL OUTER JOIN, sum 1/(k+rank)),
# extended to return the article text so the agent gets grounded context in one
# query. $1 vector literal, $2 query text, $3 candidate depth N, $4 top-K.
# --------------------------------------------------------------------------- #
SQL_SEARCH = """
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> $1::vector({dims})) AS rank
  FROM kb_faq ORDER BY embedding <=> $1::vector({dims}) LIMIT $3),
fts AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rank
  FROM kb_faq, plainto_tsquery('english', $2) q
  WHERE tsv @@ q ORDER BY ts_rank_cd(tsv, q) DESC LIMIT $3),
fused AS (
  SELECT id,
         COALESCE(1.0/({k} + vec.rank), 0) + COALESCE(1.0/({k} + fts.rank), 0) AS rrf
  FROM vec FULL OUTER JOIN fts USING (id)
  ORDER BY rrf DESC LIMIT $4)
SELECT f.rrf, k.id, k.topic, k.section, k.question, k.answer, k.chunk
FROM fused f JOIN kb_faq k USING (id)
ORDER BY f.rrf DESC
""".format(dims=config.EMBED_DIMS, k=config.RRF_K)


# --------------------------------------------------------------------------- #
# The Claude tool schema. Prescriptive about WHEN to call it — recent Claude
# models reach for tools conservatively, and this agent must ground every answer.
# --------------------------------------------------------------------------- #
SEARCH_TOOL = {
    "name": "search_knowledge_base",
    "description": (
        "Search the Choice FinX knowledge base (product FAQs on brokerage, trading, "
        "demat, mutual funds, account opening, charges, and platform how-tos). "
        "Call this BEFORE answering any product question — never answer from prior "
        "knowledge. Returns the most relevant KB articles with their chunk ids so you "
        "can cite them. Rephrase the user's question into focused keywords if helpful, "
        "and call again with a different query if the first results don't cover the ask."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query — the user's question or focused keywords.",
            },
            "top_k": {
                "type": "integer",
                "description": (
                    f"Optional number of articles to return (default {config.TOP_K}). "
                    "Raise it for broad questions, lower it for very specific ones."
                ),
            },
        },
        "required": ["query"],
    },
}


def to_pgvector(vec: list[float]) -> str:
    """Format an embedding as a pgvector literal (matches the CHO-17 helper)."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


class Retriever:
    """Owns the asyncpg pool + OpenAI client and executes the RRF search.

    Construct once at app startup (see :meth:`create`) and reuse; close on shutdown.
    """

    def __init__(self, pool: asyncpg.Pool, openai: AsyncOpenAI) -> None:
        self._pool = pool
        self._openai = openai

    @classmethod
    async def create(cls, dsn: str) -> "Retriever":
        pool = await asyncpg.create_pool(
            dsn, min_size=config.POOL_MIN, max_size=config.POOL_MAX)
        openai = AsyncOpenAI(timeout=config.OPENAI_TIMEOUT_S)
        # Fail fast if the corpus isn't the dimensionality the recipe expects.
        dims = await pool.fetchval(
            "SELECT vector_dims(embedding) FROM kb_faq WHERE embedding IS NOT NULL LIMIT 1")
        if dims != config.EMBED_DIMS:
            await pool.close()
            await openai.close()
            raise RuntimeError(
                f"kb_faq embedding dims {dims} != expected {config.EMBED_DIMS}")
        return cls(pool, openai)

    async def close(self) -> None:
        await self._pool.close()
        await self._openai.close()

    async def _embed(self, text: str) -> str:
        resp = await self._openai.embeddings.create(
            model=config.EMBED_MODEL, input=[text], dimensions=config.EMBED_DIMS)
        emb = resp.data[0].embedding
        assert len(emb) == config.EMBED_DIMS, \
            f"expected {config.EMBED_DIMS} dims, got {len(emb)}"
        return to_pgvector(emb)

    async def search(self, query: str, top_k: int | None = None) -> list[dict]:
        """Return the top-K ``kb_faq`` articles for ``query``, ranked by RRF score.

        Each result: ``{id, topic, section, question, answer, chunk, score}``.
        """
        top_k = max(1, min(top_k or config.TOP_K, 20))  # clamp model-supplied values
        vec_literal = await self._embed(query)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                SQL_SEARCH, vec_literal, query, config.N_CANDIDATES, top_k)
        return [
            {
                "id": r["id"],
                "topic": r["topic"],
                "section": r["section"],
                "question": r["question"],
                "answer": r["answer"],
                "chunk": r["chunk"],
                "score": float(r["rrf"]),
            }
            for r in rows
        ]


def format_results_for_model(results: list[dict]) -> str:
    """Render search results as the ``tool_result`` content the agent feeds Claude.

    Each article is tagged with its chunk id so the model can cite it verbatim.
    """
    if not results:
        return "No matching knowledge-base articles found."
    parts = []
    for r in results:
        parts.append(
            f"[chunk_id={r['id']} | topic={r['topic']}"
            f"{' > ' + r['section'] if r.get('section') else ''}]\n{r['chunk']}"
        )
    return "\n\n".join(parts)
