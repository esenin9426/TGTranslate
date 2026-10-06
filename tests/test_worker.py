from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app import worker
from app.models import EMBED_DIM, Source, SourceWord, Translation, User, UserWordProgress, Word, WordEmbedding
from app.services import embeddings
from app.services.fetching import FetchError

ARTICLE = (
    b"<html><head><title>Cats and Dogs</title></head><body><article>"
    b"<p>The lazy cats slept quietly on the warm windowsill while the dogs barked loudly outside the house.</p>"
    b"<p>Every morning the cats stretched slowly and the dogs chased the noisy birds across the garden.</p>"
    b"<p>Owners feed the hungry cats and walk the energetic dogs after breakfast every single day.</p>"
    b"</article></body></html>"
)

TRANSLATIONS = {"cat": "кошка", "dog": "собака", "sleep": "спать", "bark": "лаять", "bird": "птица"}


@pytest.fixture
def pipeline(monkeypatch, session_factory):
    """Point the worker at the test DB and stub the network: a canned page and a dictionary translator."""
    calls = {"fetch": 0, "translate": []}

    async def fetch_html(url):
        calls["fetch"] += 1
        return ARTICLE

    async def translate_all(words):
        calls["translate"].append(list(words))
        return {w: TRANSLATIONS[w] for w in words if w in TRANSLATIONS}

    async def embed(texts):
        calls["embed"].append(list(texts))
        return [[0.5] * EMBED_DIM for _ in texts]

    calls["embed"] = []
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "fetch_html", fetch_html)
    monkeypatch.setattr(worker, "translate_all", translate_all)
    monkeypatch.setattr(embeddings, "embed", embed)
    return calls


async def make_source(session, status="pending", **fields):
    user = await session.scalar(select(User)) or User(tg_id=42)
    session.add(user)
    await session.flush()
    source = Source(user_id=user.id, kind="url", url="https://example.com/cats", title="x", status=status, **fields)
    session.add(source)
    await session.commit()
    return source


async def test_processes_a_source_end_to_end(session, pipeline):
    source = await make_source(session)

    event = await worker.process_source(source.id)

    assert event["status"] == "ready" and event["tg_id"] == 42 and event["title"] == "Cats and Dogs"
    await session.refresh(source)
    assert source.status == "ready" and source.title == "Cats and Dogs"
    lemmas = set(await session.scalars(select(Word.lemma).join(SourceWord).where(SourceWord.source_id == source.id)))
    assert {"cat", "dog"} <= lemmas
    assert event["words"] == len(lemmas)  # words the translator did not know were dropped
    assert lemmas <= set(TRANSLATIONS)
    assert await session.scalar(select(func.count()).select_from(UserWordProgress)) == len(lemmas)


async def test_the_words_of_a_processed_source_get_meaning_vectors(session, pipeline):
    source = await make_source(session)

    await worker.process_source(source.id)

    embedded = set(await session.scalars(select(Word.lemma).join(WordEmbedding)))
    in_source = set(await session.scalars(select(Word.lemma).join(SourceWord).where(SourceWord.source_id == source.id)))
    assert embedded == in_source  # and not the words that were dropped for lack of a translation


async def test_a_source_is_ready_even_when_the_words_cannot_be_embedded(session, pipeline, monkeypatch):
    async def down(texts):
        raise httpx.ConnectError("Ollama is down")

    monkeypatch.setattr(embeddings, "embed", down)
    source = await make_source(session)

    event = await worker.process_source(source.id)

    assert event["status"] == "ready" and event["words"] > 0
    assert await session.scalar(select(func.count()).select_from(WordEmbedding)) == 0  # embed_loop catches up


async def test_stores_a_context_sentence_for_each_word(session, pipeline):
    source = await make_source(session)
    await worker.process_source(source.id)

    rows = (await session.execute(select(Word.lemma, SourceWord.context, SourceWord.form).join(SourceWord))).all()
    assert rows
    for _lemma, context, form in rows:
        assert form in context
    contexts = {lemma: context for lemma, context, _ in rows}
    assert "cats" in contexts["cat"]


async def test_translated_words_are_not_translated_again(session, pipeline):
    first = await make_source(session)
    await worker.process_source(first.id)
    translated = set(await session.scalars(select(Word.lemma).join(Translation)))
    assert translated  # the stub knows some words of the article
    pipeline["translate"].clear()
    second = await make_source(session)

    await worker.process_source(second.id)

    asked_again = {word for batch in pipeline["translate"] for word in batch}
    assert not asked_again & translated  # only the words that had no translation are retried
    assert await session.scalar(select(func.count()).select_from(Translation)) == len(translated)  # no duplicates
    assert await session.scalar(select(func.count()).where(SourceWord.source_id == second.id)) == len(translated)


async def test_duplicate_delivery_is_not_processed_or_announced_twice(session, pipeline, redis):
    source = await make_source(session)
    await worker.handle(redis, source.id)

    await worker.handle(redis, source.id)

    assert pipeline["fetch"] == 1
    assert await redis.xlen("source.processed") == 1


async def test_notification_is_resent_if_the_worker_died_before_sending_it(session, pipeline, redis):
    source = await make_source(session)
    assert (await worker.process_source(source.id))["status"] == "ready"  # saved, but the crash came before publish

    await worker.handle(redis, source.id)  # the message is redelivered

    assert await redis.xlen("source.processed") == 1
    assert pipeline["fetch"] == 1  # the notification was rebuilt from the database, nothing was downloaded again
    await session.refresh(source)
    assert source.notified


async def test_failure_notification_is_also_resent(session, pipeline, redis, monkeypatch):
    async def broken(url):
        raise FetchError("The site answered HTTP 404")

    monkeypatch.setattr(worker, "fetch_html", broken)
    source = await make_source(session)
    await worker.process_source(source.id)

    await worker.handle(redis, source.id)
    await worker.handle(redis, source.id)

    (_, fields), = await redis.xrange("source.processed")
    assert "404" in fields["data"]


async def test_internal_errors_are_not_shown_to_the_user(session, pipeline, monkeypatch):
    async def broken(url):
        raise RuntimeError("SELECT secret FROM internals WHERE password = 'hunter2'")

    monkeypatch.setattr(worker, "fetch_html", broken)
    source = await make_source(session)

    event = await worker.process_source(source.id)

    assert event["error"] == worker.INTERNAL_ERROR
    assert "hunter2" not in event["error"]


async def test_pdf_file_is_deleted_once_the_book_is_processed(session, pipeline, monkeypatch, tmp_path):
    from app.services.parsing import Extracted

    book = tmp_path / "book.pdf"
    book.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(worker, "extract_pdf", lambda path: Extracted("Book", "The lazy cats slept. " * 20))
    source = await make_source(session, file_path=str(book))
    source.kind, source.url = "pdf", None
    await session.commit()

    event = await worker.process_source(source.id)

    assert event["status"] == "ready"
    assert not book.exists()
    await session.refresh(source)
    assert source.file_path is None


async def test_failed_pdf_keeps_its_file_for_a_retry(session, pipeline, monkeypatch, tmp_path):
    def unreadable(path):
        raise ValueError("Could not read the PDF")

    book = tmp_path / "book.pdf"
    book.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(worker, "extract_pdf", unreadable)
    source = await make_source(session, file_path=str(book))
    source.kind, source.url = "pdf", None
    await session.commit()

    assert (await worker.process_source(source.id))["status"] == "failed"
    assert book.exists()


async def test_giving_up_fails_the_source_and_tells_the_user(session, pipeline, redis):
    source = await make_source(session)

    await worker.give_up(redis, source.id)

    await session.refresh(source)
    assert source.status == "failed" and source.error == worker.INTERNAL_ERROR
    assert await redis.xlen("source.processed") == 1


async def test_giving_up_never_touches_a_finished_source(session, pipeline, redis):
    source = await make_source(session, status="ready")

    await worker.give_up(redis, source.id)

    await session.refresh(source)
    assert source.status == "ready"


async def test_unknown_source_is_ignored(pipeline):
    assert await worker.process_source(999) is None


async def test_source_stuck_in_processing_is_reprocessed(session, pipeline):
    """The worker died mid-way: the replayed message must finish the job, not skip it."""
    source = await make_source(session, status="processing")

    event = await worker.process_source(source.id)

    assert event["status"] == "ready"


async def test_reprocessing_after_a_partial_run_does_not_duplicate_rows(session, pipeline):
    source = await make_source(session)
    await worker.process_source(source.id)
    count = await session.scalar(select(func.count()).select_from(SourceWord))
    source.status = "processing"  # as if the crash happened right after the writes
    await session.commit()

    await worker.process_source(source.id)

    assert await session.scalar(select(func.count()).select_from(SourceWord)) == count


async def test_download_failure_marks_the_source_failed(session, pipeline, monkeypatch):
    async def broken(url):
        raise FetchError("The site answered HTTP 404")

    monkeypatch.setattr(worker, "fetch_html", broken)
    source = await make_source(session)

    event = await worker.process_source(source.id)

    assert event["status"] == "failed" and "404" in event["error"]
    await session.refresh(source)
    assert source.status == "failed" and "404" in source.error


async def test_page_without_words_fails(session, pipeline, monkeypatch):
    async def empty(url):
        numbers = " ".join(str(n) for n in range(30))
        return f"<html><body><article><p>{numbers}</p></article></body></html>".encode()

    monkeypatch.setattr(worker, "fetch_html", empty)
    source = await make_source(session)

    event = await worker.process_source(source.id)

    assert event["status"] == "failed"


async def test_no_translations_fails_the_source(session, pipeline, monkeypatch):
    async def nothing(words):
        return {}

    monkeypatch.setattr(worker, "translate_all", nothing)
    source = await make_source(session)

    event = await worker.process_source(source.id)

    assert event["status"] == "failed" and "Translation" in event["error"]


async def test_requeue_finds_only_old_pending_sources(session, pipeline, redis):
    old = datetime.now(UTC) - timedelta(hours=1)
    lost = await make_source(session, created_at=old)
    await make_source(session)  # fresh: probably still on its way to the queue
    await make_source(session, status="ready", created_at=old)
    await make_source(session, status="failed", created_at=old)

    assert await worker.requeue_stale_sources(redis) == 1

    entries = await redis.xrange("source.submitted")
    assert [fields["data"] for _, fields in entries] == [f'{{"source_id": {lost.id}}}']


async def test_translator_outage_marks_the_source_failed_instead_of_crashing(session, pipeline, monkeypatch):
    import httpx

    async def down(words):
        raise httpx.ConnectError("ollama is down")

    monkeypatch.setattr(worker, "translate_all", down)
    source = await make_source(session)

    event = await worker.process_source(source.id)  # words were already written when this failed

    assert event["status"] == "failed" and event["tg_id"] == 42
    await session.refresh(source)
    assert source.status == "failed"
