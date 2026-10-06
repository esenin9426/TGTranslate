import json

import httpx
import pytest
from sqlalchemy import select

from app.models import EMBED_DIM, Word, WordEmbedding
from app.services import embeddings
from app.services.embeddings import embed, embed_missing


def ollama_vectors(monkeypatch, make_vector, calls=None):
    """Make the Ollama embed endpoint answer with make_vector(text) for every input text."""

    def handler(request):
        texts = json.loads(request.content)["input"]
        if calls is not None:
            calls.append(texts)
        return httpx.Response(200, json={"embeddings": [make_vector(t) for t in texts]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        embeddings.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )


async def test_vectors_of_the_wrong_size_are_a_clear_error(monkeypatch):
    ollama_vectors(monkeypatch, lambda text: [0.1] * 384)

    with pytest.raises(ValueError, match="768-dimensional"):
        await embed(["cat"])


async def test_only_words_without_a_vector_are_embedded(session, monkeypatch):
    calls = []
    ollama_vectors(monkeypatch, lambda text: [0.1] * EMBED_DIM, calls)
    words = [Word(lemma=lemma, pos="NOUN") for lemma in ("cat", "dog", "bird")]
    session.add_all(words)
    await session.flush()
    session.add(WordEmbedding(word_id=words[0].id, embedding=[0.2] * EMBED_DIM))
    await session.commit()

    added = await embed_missing(session)

    assert added == 2 and calls == [["dog", "bird"]]  # the bare lemma, no task prefix
    assert set(await session.scalars(select(WordEmbedding.word_id))) == {w.id for w in words}


async def test_embedding_can_be_limited_to_some_words_and_runs_in_batches(session, monkeypatch):
    calls = []
    ollama_vectors(monkeypatch, lambda text: [0.1] * EMBED_DIM, calls)
    monkeypatch.setattr(embeddings.settings, "embed_batch_size", 2)
    words = [Word(lemma=f"w{i}", pos="NOUN") for i in range(5)]
    session.add_all(words)
    await session.commit()

    added = await embed_missing(session, [w.id for w in words[:3]])

    assert added == 3 and calls == [["w0", "w1"], ["w2"]]
