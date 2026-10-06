import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST")  # the bot module builds a Bot at import time

import socket
from os import environ

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base, Source, SourceWord, Translation, User, UserWordProgress, Word


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    """Tests must not depend on a developer's .env."""
    monkeypatch.setattr(settings, "allow_private_urls", False)
    monkeypatch.setattr(settings, "target_lang", "ru")
    monkeypatch.setattr(settings, "correct_to_learn", 4)
    monkeypatch.setattr(settings, "reset_on_wrong", False)
    monkeypatch.setattr(settings, "recent_window", 10)


@pytest_asyncio.fixture
async def session_factory():
    """SQLite in memory by default. TEST_DATABASE_URL=postgresql+asyncpg://... runs the suite on a real Postgres
    (its tables are dropped and recreated), which enforces column sizes and NUL rules that SQLite ignores."""
    url = environ.get("TEST_DATABASE_URL")
    if url:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.run_sync(Base.metadata.drop_all)
    else:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def fake_dns(monkeypatch):
    """Resolve hostnames from a table (unknown names resolve to themselves, so IP literals work)."""
    table: dict[str, list[str]] = {}

    def getaddrinfo(host, port, *args, **kwargs):
        ips = table.get(host, [host])
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return table


async def add_words(session, source_id, user_id, words):
    """words: (lemma, pos, translation) triples. Creates the words and links them to the source."""
    ids = {}
    for lemma, pos, translation in words:
        word = Word(lemma=lemma, pos=pos)
        session.add(word)
        await session.flush()
        session.add(Translation(word_id=word.id, lang="ru", text=translation))
        session.add(SourceWord(source_id=source_id, word_id=word.id, freq=1, context=f"I {lemma} it.", form=lemma))
        session.add(UserWordProgress(user_id=user_id, source_id=source_id, word_id=word.id))
        ids[lemma] = word.id
    await session.commit()
    return ids


@pytest_asyncio.fixture
async def user_source(session):
    """A user with a ready URL source (no words yet)."""
    user = User(tg_id=1001)
    session.add(user)
    await session.flush()
    source = Source(user_id=user.id, kind="url", url="https://example.com/a", title="A", status="ready")
    session.add(source)
    await session.commit()
    return user, source
