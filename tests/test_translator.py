import json

import httpx
import pytest

from app.services import translator
from app.services.translator import OllamaTranslator, translate_all


def ollama_answer(monkeypatch, content, calls=None):
    """Make the Ollama endpoint answer with `content` (the model's raw message text)."""

    def handler(request):
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json={"message": {"content": content}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        translator.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )


async def test_translations_are_matched_by_word_and_cut_to_the_column_size(monkeypatch):
    ollama_answer(monkeypatch, json.dumps({"Cat": "кошка", "dog": "с" * 500, "other": "лишнее", "bird": 5}))

    result = await OllamaTranslator().translate(["cat", "dog", "bird"])

    assert result == {"cat": "кошка", "dog": "с" * 200}


async def test_an_answer_that_is_not_an_object_is_retried_then_reported_as_a_clear_error(monkeypatch):
    calls = []
    ollama_answer(monkeypatch, json.dumps(["кошка", "собака"]), calls)

    with pytest.raises(ValueError, match="unexpected answer"):
        await translate_all(["cat", "dog"])

    assert len(calls) == 2  # one retry
