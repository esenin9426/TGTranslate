"""Store an example sentence for every word of a source.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_words", sa.Column("context", sa.Text()))
    op.add_column("source_words", sa.Column("form", sa.String(64)))


def downgrade() -> None:
    op.drop_column("source_words", "form")
    op.drop_column("source_words", "context")
