from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import User

# The schema is managed by Alembic: run `alembic upgrade head` (docker compose does it in the `migrate` service).
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def insert_ignore(session: AsyncSession, model):
    """INSERT ... ON CONFLICT DO NOTHING for Postgres (production) and SQLite (tests)."""
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    return insert(model)


async def get_or_create_user(session: AsyncSession, tg_id: int) -> User:
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        try:
            async with session.begin_nested():
                session.add(User(tg_id=tg_id))
        except IntegrityError:  # created concurrently by another request
            pass
        await session.commit()
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
    return user
