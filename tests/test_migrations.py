"""The migrations must produce exactly the schema the models describe (checked on SQLite)."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pgvector.sqlalchemy import Vector

from app.models import Base

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


def alembic_config(db_path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    if isinstance(metadata_type, Vector):
        return False  # SQLite reads VECTOR(n) back as NUMERIC; the type itself only exists on Postgres
    return None  # the default comparison


def schema_diff(db_path) -> list:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn, opts={"compare_type": compare_type}), Base.metadata)


def columns(db_path, table) -> set[str]:
    return {c["name"] for c in sa.inspect(sa.create_engine(f"sqlite:///{db_path}")).get_columns(table)}


def test_upgrade_creates_the_schema_the_models_describe(db_path):
    command.upgrade(alembic_config(db_path), "head")

    assert schema_diff(db_path) == []
    assert {"context", "form"} <= columns(db_path, "source_words")
    assert "notified" in columns(db_path, "sources")
    assert columns(db_path, "word_embeddings") == {"word_id", "embedding"}


def test_sources_that_existed_before_the_notified_column_count_as_notified(db_path):
    cfg = alembic_config(db_path)
    command.upgrade(cfg, "0002")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, tg_id) VALUES (1, 100)"))
        conn.execute(sa.text("INSERT INTO sources (user_id, kind, title, status) VALUES (1, 'url', 't', 'ready')"))

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT notified FROM sources")) in (1, True)


def test_downgrade_removes_everything(db_path):
    cfg = alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    assert sa.inspect(sa.create_engine(f"sqlite:///{db_path}")).get_table_names() == ["alembic_version"]


def test_adopts_a_database_created_by_create_all_before_alembic(db_path):
    """Deployments from before Alembic have the tables but not the `context`/`form` columns."""
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE source_words DROP COLUMN context"))
        conn.execute(sa.text("ALTER TABLE source_words DROP COLUMN form"))
        conn.execute(sa.text("INSERT INTO users (id, tg_id) VALUES (1, 100)"))

    command.upgrade(alembic_config(db_path), "head")

    assert schema_diff(db_path) == []
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT tg_id FROM users")) == 100  # existing data survived
