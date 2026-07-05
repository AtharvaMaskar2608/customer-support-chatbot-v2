"""Read-only hybrid-RRF retrieval over ``kb_faq`` (CHO-20 ``knowledge-retrieval-tool``).

Single source of truth for the app's retrieval path. Reuses the CHO-17 recipe
verbatim — ``text-embedding-3-large``@1536 query embedding + a Postgres
dense-vector + FTS + Reciprocal-Rank-Fusion query — lifted here so the app never
imports an ``evals/`` benchmark at runtime (CHO-20 D8). The RRF SQL and constants
are kept in lockstep with ``evals/retrieval`` (see ``app.config``).

Read-only: only SELECTs against ``kb_faq``; never writes.
"""
from __future__ import annotations

from typing import Any

from . import config

# --------------------------------------------------------------------------- #
# RRF SQL — identical fusion to evals/retrieval (dense vector + FTS, 1/(k+rank)).
# $1 vector literal, $2 query text, $3 candidate depth N, $4 top-K.
# --------------------------------------------------------------------------- #
SQL_RRF = """
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> $1::vector({dims})) AS rank
  FROM kb_faq ORDER BY embedding <=> $1::vector({dims}) LIMIT $3),
fts AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rank
  FROM kb_faq, plainto_tsquery('english', $2) q
  WHERE tsv @@ q ORDER BY ts_rank_cd(tsv, q) DESC LIMIT $3)
SELECT id,
       COALESCE(1.0/({k} + vec.rank), 0) + COALESCE(1.0/({k} + fts.rank), 0) AS rrf
FROM vec FULL OUTER JOIN fts USING (id)
ORDER BY rrf DESC LIMIT $4
""".format(dims=config.EMBED_DIMS, k=config.RRF_K)


def to_pgvector(vec: list[float]) -> str:
    """Format an embedding as a pgvector literal (matches evals/retrieval)."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


class Retriever:
    """Owns the asyncpg pool + OpenAI client; exposes ``retrieve(query)``.

    Constructed once at app startup (``await Retriever.create()``), closed on
    shutdown. Heavy deps (``asyncpg``, ``openai``) are imported lazily so the
    module imports cleanly in unit tests that stub retrieval.
    """

    def __init__(self, pool: Any, client: Any) -> None:
        self._pool = pool
        self._client = client

    @classmethod
    async def create(cls, dsn: str | None = None) -> "Retriever":
        import os

        import asyncpg
        from openai import AsyncOpenAI

        dsn = dsn or os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL not set (repo .env).")
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY not set (repo .env).")
        pool = await asyncpg.create_pool(
            dsn, min_size=config.POOL_MIN, max_size=config.POOL_MAX)
        client = AsyncOpenAI(timeout=config.DB_TIMEOUT_S)
        self = cls(pool, client)
        await self._assert_dims()
        return self

    async def _assert_dims(self) -> None:
        """Fail fast if the query embedding won't match ``kb_faq.embedding``."""
        dims = await self._pool.fetchval(
            "SELECT vector_dims(embedding) FROM kb_faq "
            "WHERE embedding IS NOT NULL LIMIT 1")
        if dims != config.EMBED_DIMS:
            raise RuntimeError(
                f"kb_faq.embedding dims {dims} != expected {config.EMBED_DIMS}")

    async def _embed(self, query: str) -> str:
        resp = await self._client.embeddings.create(
            model=config.EMBED_MODEL, input=[query], dimensions=config.EMBED_DIMS)
        emb = resp.data[0].embedding
        if len(emb) != config.EMBED_DIMS:
            raise RuntimeError(
                f"embedding dims {len(emb)} != expected {config.EMBED_DIMS}")
        return to_pgvector(emb)

    async def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """Return the top-K ``kb_faq`` chunks as ``{id, topic, chunk}``.

        Embeds the query (large@1536), runs the RRF SQL, then fetches the chunk
        text/topic for the winning ids in one round-trip. Read-only.
        """
        top_k = top_k or config.TOP_K
        vec_literal = await self._embed(query)
        rows = await self._pool.fetch(
            SQL_RRF, vec_literal, query, config.N_CANDIDATES, top_k)
        ids = [r["id"] for r in rows]
        if not ids:
            return []
        # Fetch chunk text/topic, preserving RRF order.
        detail = await self._pool.fetch(
            "SELECT id, topic, chunk FROM kb_faq WHERE id = ANY($1::int[])", ids)
        by_id = {d["id"]: d for d in detail}
        out: list[dict] = []
        for i in ids:
            d = by_id.get(i)
            if d is not None:
                out.append({"id": d["id"], "topic": d["topic"], "chunk": d["chunk"]})
        return out

    async def close(self) -> None:
        await self._pool.close()
        # AsyncOpenAI has an aclose(); guard for stubs.
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


# --------------------------------------------------------------------------- #
# Claude tool schema (CHO-20 ``knowledge-retrieval-tool``: "exposed as a tool").
# --------------------------------------------------------------------------- #
SEARCH_TOOL = {
    "name": "search_knowledge_base",
    "description": (
        "Search the Choice FinX knowledge base (kb_faq) for product, account, "
        "demat, trading, corporate-action and report information. Call this for "
        "ANY product/support question before answering, and ground your answer "
        "in the returned chunks, citing their ids. Returns the most relevant "
        "knowledge-base chunks."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query, in the user's own terms.",
            },
            "top_k": {
                "type": "integer",
                "description": "How many chunks to return (default 10).",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
    },
}
