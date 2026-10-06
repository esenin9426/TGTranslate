"""Meaning vectors of lemmas (Ollama embeddings), used to offer quiz distractors that are close in meaning."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import insert_ignore
from app.models import EMBED_DIM, Word, WordEmbedding


async def embed(texts: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{settings.ollama_url}/api/embed", json={"model": settings.embed_model, "input": texts}
        )
        resp.raise_for_status()
    vectors = resp.json()["embeddings"]
    if len(vectors) != len(texts) or any(len(v) != EMBED_DIM for v in vectors):
        raise ValueError(f"{settings.embed_model} does not return {EMBED_DIM}-dimensional vectors")
    return vectors


async def embed_missing(session: AsyncSession, word_ids: list[int] | None = None, limit: int | None = None) -> int:
    """Store vectors for the words that have none yet (only `word_ids` if given). Returns how many were added.

    The bare lemma is embedded: a task prefix ("clustering: ...") squeezes all distances together and makes
    synonyms harder to tell from merely related words.
    """
    stmt = (
        select(Word.id, Word.lemma)
        .outerjoin(WordEmbedding, WordEmbedding.word_id == Word.id)
        .where(WordEmbedding.word_id.is_(None))
        .order_by(Word.id)
    )
    if word_ids is not None:
        stmt = stmt.where(Word.id.in_(word_ids))
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).all()

    size = settings.embed_batch_size
    for i in range(0, len(rows), size):
        batch = rows[i : i + size]
        vectors = await embed([lemma for _, lemma in batch])
        await session.execute(
            insert_ignore(session, WordEmbedding)
            .values([{"word_id": word_id, "embedding": v} for (word_id, _), v in zip(batch, vectors, strict=True)])
            .on_conflict_do_nothing()
        )
        await session.commit()  # per batch: a failure later keeps what is done
    return len(rows)
