"""Initial schema.

Tables that already exist are skipped: deployments started before Alembic was introduced created them
with `create_all`, and this revision must adopt such a database instead of failing on it.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NOW = sa.func.now()


def _create(name: str, *elements) -> None:
    if not sa.inspect(op.get_bind()).has_table(name):
        op.create_table(name, *elements)


def upgrade() -> None:
    _create(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )
    _create(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )
    _create(
        "words",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lemma", sa.String(64), nullable=False),
        sa.Column("pos", sa.String(8), nullable=False),
    )
    _create(
        "translations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("word_id", sa.Integer(), sa.ForeignKey("words.id"), nullable=False),
        sa.Column("lang", sa.String(8), nullable=False),
        sa.Column("text", sa.String(200), nullable=False),
        sa.UniqueConstraint("word_id", "lang"),
    )
    _create(
        "source_words",
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("word_id", sa.Integer(), sa.ForeignKey("words.id"), primary_key=True),
        sa.Column("freq", sa.Integer(), nullable=False),
    )
    _create(
        "user_word_progress",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("word_id", sa.Integer(), sa.ForeignKey("words.id"), primary_key=True),
        sa.Column("correct_count", sa.Integer(), nullable=False),
        sa.Column("wrong_count", sa.Integer(), nullable=False),
        sa.Column("last_asked_at", sa.DateTime(timezone=True)),
    )

    inspector = sa.inspect(op.get_bind())
    for table, name, columns, unique in [
        ("users", "ix_users_tg_id", ["tg_id"], True),
        ("sources", "ix_sources_user_id", ["user_id"], False),
        ("words", "ix_words_lemma", ["lemma"], True),
        ("translations", "ix_translations_word_id", ["word_id"], False),
    ]:
        if name not in {ix["name"] for ix in inspector.get_indexes(table)}:
            op.create_index(name, table, columns, unique=unique)


def downgrade() -> None:
    for table in ("user_word_progress", "source_words", "translations", "words", "sources", "users"):
        op.drop_table(table)
