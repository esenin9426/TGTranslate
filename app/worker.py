"""Queue consumer: parse a submitted source, translate its words, store them, notify the user."""

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select, update

from app.config import settings
from app.db import SessionLocal, insert_ignore
from app.messaging import consume, publish
from app.models import TITLE_MAX, Source, SourceWord, Translation, User, UserWordProgress, Word
from app.services.embeddings import embed_missing
from app.services.fetching import fetch_html
from app.services.nlp import extract_lemmas
from app.services.parsing import extract_html, extract_pdf
from app.services.translator import translate_all

log = logging.getLogger("worker")

GROUP = "worker"
# The consumer name (settings.worker_consumer) must stay the same across restarts: unacknowledged messages
# belong to a consumer *name*, so a restarted worker replays the source it died on.
MAX_ATTEMPTS = 3  # a source whose processing keeps crashing is given up on instead of blocking the queue
INTERNAL_ERROR = "Internal error while processing the source"


def user_message(exc: Exception) -> str:
    """The reason shown to the user. Only errors we raise ourselves are passed on: anything else (a database
    error, a library bug) may contain SQL, paths or other internals, so it is logged and replaced."""
    if isinstance(exc, ValueError):  # includes FetchError and UnsafeURL
        return str(exc)[:500]
    if isinstance(exc, httpx.HTTPError):
        return "The translation service is unavailable, try again later"
    return INTERNAL_ERROR


async def _ready_event(session, source: Source, tg_id: int) -> dict:
    words = await session.scalar(select(func.count()).where(SourceWord.source_id == source.id))
    return {"tg_id": tg_id, "source_id": source.id, "status": "ready", "title": source.title, "words": words or 0}


def _failed_event(source: Source, tg_id: int) -> dict:
    return {"tg_id": tg_id, "source_id": source.id, "status": "failed",
            "title": source.title or source.url or "", "error": source.error}


async def process_source(source_id: int) -> dict | None:
    """Returns the notification event, or None if there is nothing to do."""
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return None
        user = await session.get(User, source.user_id)
        tg_id = user.tg_id  # read now: rollback() expires `user`, and a lazy reload is impossible in async code
        if source.status in ("ready", "learned", "failed"):
            # Duplicate delivery. Nothing to redo, but if the worker died after saving the result and before the
            # user was told, this is the chance to send the notification again.
            if source.notified:
                return None
            if source.status == "failed":
                return _failed_event(source, tg_id)
            return await _ready_event(session, source, tg_id)
        # A "processing" status here means an earlier attempt died mid-way: it is safe to run again,
        # every write below is idempotent.
        source.status = "processing"
        await session.commit()

        try:
            if source.kind == "url":
                html = await fetch_html(source.url)
                extracted = await asyncio.to_thread(extract_html, html, source.url)
            else:
                extracted = await asyncio.to_thread(extract_pdf, source.file_path)
            lemmas = await asyncio.to_thread(extract_lemmas, extracted.text, settings.max_words_per_source)
            if not lemmas:
                raise ValueError("No learnable words found")

            await session.execute(
                insert_ignore(session, Word)
                .values([{"lemma": info.lemma, "pos": info.pos} for info in lemmas])
                .on_conflict_do_nothing(index_elements=["lemma"])
            )
            words = {
                w.lemma: w
                for w in await session.scalars(select(Word).where(Word.lemma.in_([info.lemma for info in lemmas])))
            }

            have = set(
                await session.scalars(
                    select(Translation.word_id).where(
                        Translation.word_id.in_([w.id for w in words.values()]),
                        Translation.lang == settings.target_lang,
                    )
                )
            )
            missing = [lemma for lemma, w in words.items() if w.id not in have]
            translated = await translate_all(missing) if missing else {}
            if translated:
                await session.execute(
                    insert_ignore(session, Translation)
                    .values(
                        [
                            {"word_id": words[lemma].id, "lang": settings.target_lang, "text": text}
                            for lemma, text in translated.items()
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["word_id", "lang"])
                )
                have |= {words[lemma].id for lemma in translated}

            # Words without a translation cannot be quizzed, so they are dropped.
            usable = [info for info in lemmas if words[info.lemma].id in have]
            if not usable:
                raise ValueError("Translation produced no results")

            await session.execute(
                insert_ignore(session, SourceWord)
                .values(
                    [
                        {
                            "source_id": source.id,
                            "word_id": words[info.lemma].id,
                            "freq": info.freq,
                            "context": info.context,
                            "form": info.form,
                        }
                        for info in usable
                    ]
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                insert_ignore(session, UserWordProgress)
                .values([{"user_id": user.id, "source_id": source.id, "word_id": words[i.lemma].id} for i in usable])
                .on_conflict_do_nothing()
            )
            source.title = extracted.title[:TITLE_MAX]
            source.status = "ready"
            source.error = None
            uploaded, source.file_path = (source.file_path if source.kind == "pdf" else None), None
            await session.commit()
            if uploaded:  # the book is in the database now; a failed source keeps its file so it can be retried
                Path(uploaded).unlink(missing_ok=True)
        except Exception as exc:
            log.exception("Failed to process source %s", source_id)
            await session.rollback()
            source = await session.get(Source, source_id)
            source.status = "failed"
            source.error = user_message(exc)
            await session.commit()
            return _failed_event(source, tg_id)

        event = await _ready_event(session, source, tg_id)  # before the rollback below expires `source`
        # Optional: without vectors the quiz picks plainer distractors, and embed_loop retries later.
        try:
            await embed_missing(session, [words[info.lemma].id for info in usable])
        except Exception:
            log.warning("Could not embed the words of source %s", source_id, exc_info=True)
            await session.rollback()
        return event


async def mark_notified(source_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.id == source_id).values(notified=True))
        await session.commit()


async def handle(redis: Redis, source_id: int) -> None:
    """Process one source and tell the user; the source is marked as notified only once the event is queued."""
    event = await process_source(source_id)
    if event:
        await publish(redis, settings.stream_processed, event)
        await mark_notified(source_id)


async def give_up(redis: Redis, source_id: int) -> None:
    """Processing crashed MAX_ATTEMPTS times in a row: fail the source so the message stops blocking the queue."""
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        if source is None or source.status in ("ready", "learned"):
            return
        source.status, source.error = "failed", INTERNAL_ERROR
        await session.commit()
    try:
        await handle(redis, source_id)
    except Exception:
        log.exception("Could not notify the user about source %s", source_id)


async def requeue_stale_sources(redis: Redis) -> int:
    """Re-enqueue sources that were saved but never reached the queue (a crash between the two steps).

    Sources that are merely waiting behind a long job may be enqueued twice; that is harmless because
    process_source ignores a source that is already done.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.stale_pending_seconds)
    async with SessionLocal() as session:
        ids = list(
            await session.scalars(select(Source.id).where(Source.status == "pending", Source.created_at < cutoff))
        )
    for source_id in ids:
        await publish(redis, settings.stream_submitted, {"source_id": source_id})
    return len(ids)


async def requeue_loop(redis: Redis) -> None:
    """Periodically rescue sources lost between the database commit and the queue (an API crash while we run)."""
    while True:
        try:
            log.info("Re-enqueued %d stale sources", await requeue_stale_sources(redis))
        except Exception:
            log.exception("Could not re-enqueue stale sources")
        await asyncio.sleep(settings.stale_pending_seconds)


async def embed_loop() -> None:
    """Give vectors to words that have none: words from before embeddings existed, or from a time Ollama was down."""
    while True:
        try:
            async with SessionLocal() as session:
                while added := await embed_missing(session, limit=settings.embed_batch_size):
                    log.info("Embedded %d words", added)
        except Exception:
            log.warning("Could not embed words, retrying later", exc_info=True)
        await asyncio.sleep(settings.embed_backfill_seconds)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # socket_timeout must exceed consume()'s block_ms
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=30)
    requeuer = asyncio.create_task(requeue_loop(redis))
    embedder = asyncio.create_task(embed_loop())
    attempts: Counter[int] = Counter()
    try:
        while True:
            try:
                async for event in consume(redis, settings.stream_submitted, GROUP, settings.worker_consumer):
                    source_id = event.get("source_id") if isinstance(event, dict) else None
                    if not isinstance(source_id, int):
                        log.error("Ignoring a message without a source id: %r", event)
                        continue
                    try:
                        await handle(redis, source_id)
                    except Exception:
                        attempts[source_id] += 1
                        if attempts[source_id] < MAX_ATTEMPTS:
                            raise
                        log.exception("Giving up on source %s after %d attempts", source_id, MAX_ATTEMPTS)
                        await give_up(redis, source_id)
                    attempts.pop(source_id, None)
            except Exception:
                # The failed message was not acknowledged, so the next consume() replays it first.
                log.exception("Worker loop failed, retrying in 5 s")
                await asyncio.sleep(5)
    finally:
        requeuer.cancel()
        embedder.cancel()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
