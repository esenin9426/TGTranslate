"""Meaning vectors of words, used to pick quiz distractors that are close in meaning.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

EMBED_DIM = 768  # app.models.EMBED_DIM at the time of this revision


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")  # needs the pgvector/pgvector Postgres image
    if sa.inspect(bind).has_table("word_embeddings"):
        return  # the table was just created from the current models
    op.create_table(
        "word_embeddings",
        sa.Column("word_id", sa.Integer(), sa.ForeignKey("words.id"), primary_key=True),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("word_embeddings")  # the extension stays: other databases on the server may use it
