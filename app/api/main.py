import asyncio
import hmac
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, HttpUrl
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, get_or_create_user
from app.messaging import publish
from app.models import TITLE_MAX, Source, SourceWord, User, UserWordProgress
from app.services import quiz
from app.services.fetching import FetchError, UnsafeURL, validate_url

UPLOAD_CHUNK = 1024 * 1024
ACTIVE = ("pending", "processing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    yield
    await app.state.redis.aclose()


def verify_token(request: Request, x_api_token: str = Header(default="")) -> None:
    """The API trusts the `tg_id` it is given, so only the bot (which knows the shared secret) may call it."""
    if request.url.path == "/health" or not settings.api_token:
        return
    if not hmac.compare_digest(x_api_token.encode(), settings.api_token.encode()):
        raise HTTPException(401, "Invalid API token")


app = FastAPI(title="TGTranslate", lifespan=lifespan, dependencies=[Depends(verify_token)])


async def get_session():
    async with SessionLocal() as session:
        yield session


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


@app.get("/health")
async def health():
    return {"ok": True}


async def _enqueue(redis: Redis, session: AsyncSession, source: Source) -> None:
    try:
        await publish(redis, settings.stream_submitted, {"source_id": source.id})
    except Exception:
        # Do not leave a "pending" source nobody will ever process.
        source.status = "failed"
        source.error = "The processing queue is unavailable"
        source.notified = True  # the user gets the 503 right now
        await session.commit()
        raise HTTPException(503, "The processing queue is unavailable, try again later") from None


async def _check_capacity(session: AsyncSession, user: User) -> None:
    active = await session.scalar(
        select(func.count()).where(Source.user_id == user.id, Source.status.in_(ACTIVE))
    )
    if (active or 0) >= settings.max_active_sources:
        raise HTTPException(429, "Too many sources are being processed already, wait until they are ready")


async def _submit(redis: Redis, session: AsyncSession, user: User, **fields) -> dict:
    await _check_capacity(session, user)
    source = Source(user_id=user.id, status="pending", **fields)
    session.add(source)
    await session.commit()
    await _enqueue(redis, session, source)
    return {"source_id": source.id, "status": source.status}


class UrlIn(BaseModel):
    tg_id: int
    url: HttpUrl


@app.post("/sources/url")
async def add_url(body: UrlIn, redis: Redis = Depends(get_redis), session: AsyncSession = Depends(get_session)):
    url = str(body.url)
    try:
        await validate_url(url)
    except (UnsafeURL, FetchError) as exc:
        raise HTTPException(400, str(exc)) from exc
    user = await get_or_create_user(session, body.tg_id)
    existing = await session.scalar(
        select(Source).where(Source.user_id == user.id, Source.url == url, Source.status != "failed")
    )
    if existing:  # the same link again: do not parse and translate it twice
        return {"source_id": existing.id, "status": existing.status, "duplicate": True}
    return await _submit(redis, session, user, kind="url", url=url, title=url[:TITLE_MAX])


async def _save_pdf(file: UploadFile) -> Path:
    """Store the upload, enforcing the size limit and the PDF signature."""
    limit = settings.max_pdf_mb * 1024 * 1024
    path = Path(settings.upload_dir) / f"{uuid.uuid4().hex}.pdf"
    size = 0
    try:
        with path.open("wb") as out:
            while chunk := await file.read(UPLOAD_CHUNK):
                if size == 0 and not chunk.startswith(b"%PDF-"):
                    raise HTTPException(400, "The file is not a PDF")
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"The PDF is larger than {settings.max_pdf_mb} MB")
                await asyncio.to_thread(out.write, chunk)  # do not block the event loop on disk I/O
        if size == 0:
            raise HTTPException(400, "The file is empty")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


@app.post("/sources/pdf")
async def add_pdf(
    tg_id: int = Form(),
    file: UploadFile = File(),
    redis: Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(session, tg_id)
    await _check_capacity(session, user)  # before saving anything to disk
    path = await _save_pdf(file)
    title = Path(file.filename or "book").stem[:TITLE_MAX]
    try:
        return await _submit(redis, session, user, kind="pdf", file_path=str(path), title=title)
    except BaseException:  # the source was not created (or not queued): the file would stay forever
        path.unlink(missing_ok=True)
        raise


@app.get("/sources")
async def list_sources(tg_id: int, session: AsyncSession = Depends(get_session)):
    """The most recent sources of the user (the bot shows them as buttons, and Telegram allows 100 at most)."""
    user = await get_or_create_user(session, tg_id)
    sources = (
        await session.scalars(
            select(Source).where(Source.user_id == user.id).order_by(Source.id.desc()).limit(settings.sources_page_size)
        )
    ).all()
    progress = await quiz.sources_progress(session, user.id, [s.id for s in sources])
    return [
        {"id": s.id, "title": s.title, "kind": s.kind, "status": s.status, **progress[s.id]} for s in sources
    ]


async def _own_source(session: AsyncSession, tg_id: int, source_id: int) -> tuple[User, Source]:
    user = await get_or_create_user(session, tg_id)
    source = await session.get(Source, source_id)
    if source is None or source.user_id != user.id:
        raise HTTPException(404, "Source not found")
    return user, source


@app.post("/sources/{source_id}/retry")
async def retry_source(
    source_id: int, tg_id: int, redis: Redis = Depends(get_redis), session: AsyncSession = Depends(get_session)
):
    """Process a failed source again."""
    user, source = await _own_source(session, tg_id, source_id)
    if source.status != "failed":
        raise HTTPException(409, f"Source is {source.status}")
    if source.kind == "pdf" and not (source.file_path and Path(source.file_path).exists()):
        raise HTTPException(409, "The uploaded file is no longer available, send the PDF again")
    await _check_capacity(session, user)
    source.status, source.error, source.notified = "pending", None, False
    await session.commit()
    await _enqueue(redis, session, source)
    return {"source_id": source.id, "status": source.status}


@app.delete("/sources/{source_id}")
async def delete_source(
    source_id: int, tg_id: int, redis: Redis = Depends(get_redis), session: AsyncSession = Depends(get_session)
):
    user, source = await _own_source(session, tg_id, source_id)
    if source.status == "processing":
        raise HTTPException(409, "The source is being processed right now")
    file_path = source.file_path
    # Explicit deletes: ON DELETE CASCADE is not enforced by every database the tests run on.
    await session.execute(delete(UserWordProgress).where(UserWordProgress.source_id == source.id))
    await session.execute(delete(SourceWord).where(SourceWord.source_id == source.id))
    await session.delete(source)
    await session.commit()
    if file_path:
        Path(file_path).unlink(missing_ok=True)
    await quiz.forget_source(redis, user.id, source_id)
    return {"deleted": source_id}


class QuizNextIn(BaseModel):
    tg_id: int
    source_id: int


@app.post("/quiz/next")
async def quiz_next(
    body: QuizNextIn, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis)
):
    user = await get_or_create_user(session, body.tg_id)
    source = await session.get(Source, body.source_id)
    if source is None or source.user_id != user.id:
        raise HTTPException(404, "Source not found")
    if source.status not in ("ready", "learned"):
        raise HTTPException(409, f"Source is {source.status}")

    question = await quiz.next_question(session, redis, user, source.id)
    progress = await quiz.source_progress(session, user.id, source.id)
    return {"done": question is None, "question": question, "title": source.title, **progress}


class AnswerIn(BaseModel):
    tg_id: int
    word_id: int
    option_index: int


@app.post("/quiz/answer")
async def quiz_answer(
    body: AnswerIn, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis)
):
    user = await get_or_create_user(session, body.tg_id)
    try:
        return await quiz.submit_answer(session, redis, user, body.word_id, body.option_index)
    except quiz.StaleQuestion:
        raise HTTPException(409, "This question is no longer active") from None
