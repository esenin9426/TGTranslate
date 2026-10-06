"""Track whether the user was notified about a source's outcome.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "notified" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("sources")}:
        return  # the table was just created from the current models
    # server_default=true: sources that exist already were handled (or notified) before this column appeared.
    op.add_column("sources", sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("sources", "notified")
