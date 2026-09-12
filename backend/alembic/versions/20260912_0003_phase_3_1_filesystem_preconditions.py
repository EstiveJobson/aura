"""Bind pending write approvals to filesystem preconditions.

Revision ID: 20260912_0003
Revises: 20260911_0002
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260912_0003"
down_revision: str | None = "20260911_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("filesystem_preconditions", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("approvals", "filesystem_preconditions")
