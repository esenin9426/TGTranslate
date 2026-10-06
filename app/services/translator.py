import asyncio
import json
from typing import Protocol

import httpx

from app.config import settings
from app.models import TRANSLATION_MAX


class Translator(Protocol):
    async def translate(self, words: list[str]) -> dict[str, str]:
        """Return {english word: translation}. Words that could not be translated are omitted."""


class OllamaTranslator:
    async def translate(self, words: list[str]) -> dict[str, str]:
        system = (
            f"You translate English words into {settings.target_lang_name}. "
            "Reply with a single JSON object mapping every input word to its most common "
            "translation (one to three words, no explanations)."
        )
        payload = {
            "model": settings.ollama_model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(words, ensure_ascii=False)},
            ],
        }
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
            resp.raise_for_status()
        data = json.loads(resp.json()["message"]["content"])
        if not isinstance(data, dict):
            raise ValueError("The translation model returned an unexpected answer")
        wanted = set(words)
        return {
            k.lower(): v.strip()[:TRANSLATION_MAX]
            for k, v in data.items()
            if k.lower() in wanted and isinstance(v, str) and v.strip()
        }


class ArgosTranslator:
    """Offline fallback. Needs the en->target language pack installed (see README)."""

    async def translate(self, words: list[str]) -> dict[str, str]:
        from argostranslate import translate as argos

        def run() -> dict[str, str]:
            out = {}
            for w in words:
                t = argos.translate(w, "en", settings.target_lang).strip().lower()
                if t and t != w:
                    out[w] = t[:TRANSLATION_MAX]
            return out

        return await asyncio.to_thread(run)


def get_translator() -> Translator:
    return ArgosTranslator() if settings.translator == "argos" else OllamaTranslator()


async def translate_all(words: list[str]) -> dict[str, str]:
    """Translate in batches; each batch is retried once."""
    translator = get_translator()
    result: dict[str, str] = {}
    size = settings.translate_batch_size
    for i in range(0, len(words), size):
        batch = words[i : i + size]
        for attempt in range(2):
            try:
                result.update(await translator.translate(batch))
                break
            except (httpx.HTTPError, ValueError, KeyError):  # ValueError includes JSONDecodeError
                if attempt == 1:
                    raise
    return result
