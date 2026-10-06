import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.main import app, get_redis, get_session
from app.config import settings
from app.models import Source

from .conftest import add_words

PDF = b"%PDF-1.4\n%fake but with the right signature\n"
PUBLIC_IP = "93.184.216.34"


@pytest.fixture
async def client(session_factory, redis, monkeypatch, tmp_path, fake_dns):
    async def override_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_redis] = lambda: redis
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    fake_dns["example.com"] = [PUBLIC_IP]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def queued(redis):
    return [fields["data"] for _, fields in await redis.xrange(settings.stream_submitted)]


# --- URLs -------------------------------------------------------------------------------------------------


async def test_url_is_saved_and_queued(client, redis, session):
    resp = await client.post("/sources/url", json={"tg_id": 7, "url": "https://example.com/article"})

    assert resp.status_code == 200
    source_id = resp.json()["source_id"]
    assert await queued(redis) == [f'{{"source_id": {source_id}}}']
    source = await session.get(Source, source_id)
    assert source.status == "pending" and source.url == "https://example.com/article"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/health",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://redis:6379/",
        "ftp://example.com/x",
    ],
)
async def test_internal_urls_are_rejected_and_nothing_is_queued(client, redis, session, fake_dns, url):
    fake_dns["localhost"] = ["127.0.0.1"]
    fake_dns["redis"] = ["172.18.0.3"]

    resp = await client.post("/sources/url", json={"tg_id": 7, "url": url})

    assert resp.status_code in (400, 422)
    assert await queued(redis) == []
    assert (await session.scalars(select(Source))).all() == []


async def test_malformed_url_is_rejected(client):
    assert (await client.post("/sources/url", json={"tg_id": 7, "url": "not a url"})).status_code == 422


async def test_queue_outage_fails_the_source_and_reports_503(client, redis, session, monkeypatch):
    async def broken(*args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(redis, "xadd", broken)

    resp = await client.post("/sources/url", json={"tg_id": 7, "url": "https://example.com/a"})

    assert resp.status_code == 503
    (source,) = (await session.scalars(select(Source))).all()
    assert source.status == "failed"  # not stuck in "pending" forever


# --- PDFs -------------------------------------------------------------------------------------------------


def upload(content, name="book.pdf", tg_id="7"):
    return {"data": {"tg_id": tg_id}, "files": {"file": (name, content, "application/pdf")}}


async def test_pdf_is_saved_and_queued(client, redis, session, tmp_path):
    resp = await client.post("/sources/pdf", **upload(PDF))

    assert resp.status_code == 200
    source = await session.get(Source, resp.json()["source_id"])
    assert source.kind == "pdf" and source.title == "book"
    with open(source.file_path, "rb") as f:
        assert f.read() == PDF
    assert len(await queued(redis)) == 1


async def test_non_pdf_content_is_rejected(client, redis, tmp_path):
    resp = await client.post("/sources/pdf", **upload(b"MZ\x90\x00 an executable pretending to be a book", "book.pdf"))

    assert resp.status_code == 400
    assert list(tmp_path.iterdir()) == []  # nothing left on disk
    assert await queued(redis) == []


async def test_empty_upload_is_rejected(client, tmp_path):
    assert (await client.post("/sources/pdf", **upload(b""))).status_code == 400
    assert list(tmp_path.iterdir()) == []


async def test_oversized_pdf_is_rejected_and_cleaned_up(client, redis, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_pdf_mb", 1)

    resp = await client.post("/sources/pdf", **upload(PDF + b"0" * (1024 * 1024)))

    assert resp.status_code == 413
    assert list(tmp_path.iterdir()) == []
    assert await queued(redis) == []


async def test_pdf_exactly_at_the_limit_is_accepted(client, monkeypatch):
    monkeypatch.setattr(settings, "max_pdf_mb", 1)
    assert (await client.post("/sources/pdf", **upload(PDF + b"0" * (1024 * 1024 - len(PDF))))).status_code == 200


async def test_pdf_file_is_removed_when_the_queue_is_down(client, redis, tmp_path, monkeypatch):
    async def broken(*args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(redis, "xadd", broken)

    resp = await client.post("/sources/pdf", **upload(PDF))

    assert resp.status_code == 503
    assert list(tmp_path.iterdir()) == []


# --- quiz -------------------------------------------------------------------------------------------------


async def test_quiz_flow_over_http(client, session, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, [(f"noun{i}", "NOUN", f"сущ{i}") for i in range(5)])

    resp = await client.post("/quiz/next", json={"tg_id": user.tg_id, "source_id": source.id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["done"] is False and data["total_words"] == 5
    question = data["question"]
    assert question["pos"] == "NOUN" and question["context"] and question["form"]
    assert len(question["options"]) == 4

    correct = question["options"].index(f"сущ{question['word'][-1]}")
    answered = await client.post(
        "/quiz/answer", json={"tg_id": user.tg_id, "word_id": question["word_id"], "option_index": correct}
    )
    assert answered.status_code == 200 and answered.json()["correct"] is True

    repeated = await client.post(
        "/quiz/answer", json={"tg_id": user.tg_id, "word_id": question["word_id"], "option_index": correct}
    )
    assert repeated.status_code == 409


async def test_quiz_is_unavailable_until_the_source_is_processed(client, session, user_source):
    user, source = user_source
    source.status = "processing"
    await session.commit()

    resp = await client.post("/quiz/next", json={"tg_id": user.tg_id, "source_id": source.id})

    assert resp.status_code == 409


async def test_users_cannot_open_each_others_sources(client, user_source):
    _, source = user_source
    resp = await client.post("/quiz/next", json={"tg_id": 555, "source_id": source.id})
    assert resp.status_code == 404


async def test_sources_list_shows_progress(client, session, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, [("cat", "NOUN", "кошка")])

    (item,) = (await client.get("/sources", params={"tg_id": user.tg_id})).json()

    assert item["id"] == source.id and item["total_words"] == 1 and item["learned_words"] == 0


# --- access control, limits ---------------------------------------------------------------------------------


async def test_api_requires_the_shared_token_when_one_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "s3cret")

    assert (await client.get("/sources", params={"tg_id": 1})).status_code == 401
    assert (await client.get("/sources", params={"tg_id": 1}, headers={"X-API-Token": "wrong"})).status_code == 401
    assert (await client.get("/sources", params={"tg_id": 1}, headers={"X-API-Token": "s3cret"})).status_code == 200
    assert (await client.get("/health")).status_code == 200  # the container healthcheck has no token


async def test_a_long_url_does_not_overflow_the_title_column(client, session):
    url = "https://example.com/a?" + "utm=x&" * 100

    resp = await client.post("/sources/url", json={"tg_id": 7, "url": url})

    assert resp.status_code == 200
    assert len((await session.get(Source, resp.json()["source_id"])).title) <= 300


async def test_the_same_link_is_not_queued_twice(client, redis):
    first = await client.post("/sources/url", json={"tg_id": 7, "url": "https://example.com/a"})
    again = await client.post("/sources/url", json={"tg_id": 7, "url": "https://example.com/a"})

    assert again.json()["source_id"] == first.json()["source_id"] and again.json()["duplicate"] is True
    assert len(await queued(redis)) == 1


async def test_the_same_link_from_another_user_is_a_separate_source(client, redis):
    await client.post("/sources/url", json={"tg_id": 7, "url": "https://example.com/a"})
    other = await client.post("/sources/url", json={"tg_id": 8, "url": "https://example.com/a"})

    assert "duplicate" not in other.json()
    assert len(await queued(redis)) == 2


async def test_too_many_active_sources_are_refused(client, redis, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "max_active_sources", 2)
    for n in range(2):
        resp = await client.post("/sources/url", json={"tg_id": 7, "url": f"https://example.com/{n}"})
        assert resp.status_code == 200

    assert (await client.post("/sources/url", json={"tg_id": 7, "url": "https://example.com/3"})).status_code == 429
    refused = await client.post("/sources/pdf", **upload(PDF))
    assert refused.status_code == 429
    assert list(tmp_path.iterdir()) == []  # the upload was not even written
    assert (await client.post("/sources/url", json={"tg_id": 8, "url": "https://example.com/0"})).status_code == 200


async def test_sources_list_is_limited_to_the_latest_ones(client, session, user_source, monkeypatch):
    user, _ = user_source
    monkeypatch.setattr(settings, "sources_page_size", 3)
    for n in range(5):
        session.add(Source(user_id=user.id, kind="url", url=f"https://example.com/{n}", title=str(n), status="ready"))
    await session.commit()

    items = (await client.get("/sources", params={"tg_id": user.tg_id})).json()

    assert [i["title"] for i in items] == ["4", "3", "2"]


# --- retry and delete ---------------------------------------------------------------------------------------


async def failed_source(session, user_source, **fields):
    _, source = user_source
    source.status, source.error, source.notified = "failed", "boom", True
    for name, value in fields.items():
        setattr(source, name, value)
    await session.commit()
    return source


async def test_failed_source_can_be_retried(client, session, redis, user_source):
    source = await failed_source(session, user_source)

    resp = await client.post(f"/sources/{source.id}/retry", params={"tg_id": 1001})

    assert resp.status_code == 200
    await session.refresh(source)
    assert source.status == "pending" and source.error is None and source.notified is False
    assert await queued(redis) == [f'{{"source_id": {source.id}}}']


async def test_only_failed_sources_can_be_retried(client, user_source):
    _, source = user_source  # "ready"
    assert (await client.post(f"/sources/{source.id}/retry", params={"tg_id": 1001})).status_code == 409


async def test_a_pdf_whose_file_is_gone_cannot_be_retried(client, session, user_source):
    source = await failed_source(session, user_source, kind="pdf", file_path="/nonexistent/book.pdf")
    assert (await client.post(f"/sources/{source.id}/retry", params={"tg_id": 1001})).status_code == 409


async def test_users_cannot_retry_or_delete_each_others_sources(client, session, user_source):
    source = await failed_source(session, user_source)
    assert (await client.post(f"/sources/{source.id}/retry", params={"tg_id": 555})).status_code == 404
    assert (await client.delete(f"/sources/{source.id}", params={"tg_id": 555})).status_code == 404


async def test_deleting_a_source_removes_its_words_progress_and_file(client, session, redis, user_source, tmp_path):
    from sqlalchemy import func

    from app.models import SourceWord, UserWordProgress

    user, source = user_source
    book = tmp_path / "book.pdf"
    book.write_bytes(PDF)
    source.file_path = str(book)
    await add_words(session, source.id, user.id, [("cat", "NOUN", "кошка")])
    await redis.lpush(f"recent:{user.id}:{source.id}", 1)

    resp = await client.delete(f"/sources/{source.id}", params={"tg_id": user.tg_id})

    assert resp.status_code == 200
    for model in (Source, SourceWord, UserWordProgress):
        assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert not book.exists()
    assert await redis.llen(f"recent:{user.id}:{source.id}") == 0


async def test_a_source_being_processed_cannot_be_deleted(client, session, user_source):
    _, source = user_source
    source.status = "processing"
    await session.commit()
    assert (await client.delete(f"/sources/{source.id}", params={"tg_id": 1001})).status_code == 409


async def test_answer_to_a_question_of_a_deleted_source_is_stale(client, session, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, [(f"noun{i}", "NOUN", f"сущ{i}") for i in range(4)])
    question = (await client.post("/quiz/next", json={"tg_id": user.tg_id, "source_id": source.id})).json()["question"]

    await client.delete(f"/sources/{source.id}", params={"tg_id": user.tg_id})

    resp = await client.post(
        "/quiz/answer", json={"tg_id": user.tg_id, "word_id": question["word_id"], "option_index": 0}
    )
    assert resp.status_code == 409
