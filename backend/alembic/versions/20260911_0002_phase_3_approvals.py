"""Add Phase 3 approval lifecycle states and records.

Revision ID: 20260911_0002
Revises: 20260911_0001
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260911_0002"
down_revision: str | None = "20260911_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("task_status", "tasks", type_="check")
    op.alter_column(
        "tasks",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "task_status",
        "tasks",
        "status IN ('PENDING', 'PLANNING', 'WAITING_FOR_APPROVAL', "
        "'EXECUTING', 'SUCCEEDED', 'REJECTED', 'FAILED')",
    )
    op.drop_constraint("execution_status", "executions", type_="check")
    op.alter_column(
        "executions",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "execution_status",
        "executions",
        "status IN ('WAITING_FOR_APPROVAL', 'RUNNING', 'SUCCEEDED', 'REJECTED', 'FAILED')",
    )
    op.drop_constraint("tool_call_status", "tool_calls", type_="check")
    op.alter_column(
        "tool_calls",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "tool_call_status",
        "tool_calls",
        "status IN ('WAITING_FOR_APPROVAL', 'RUNNING', 'SUCCEEDED', 'REJECTED', 'FAILED')",
    )

    approval_decision = sa.Enum(
        "PENDING",
        "APPROVED",
        "REJECTED",
        name="approval_decision",
        native_enum=False,
        create_constraint=True,
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("decision", approval_decision, nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id"),
    )


def downgrade() -> None:
    op.drop_table("approvals")

    op.execute(
        "UPDATE tasks SET status = 'FAILED' WHERE status IN ('WAITING_FOR_APPROVAL', 'REJECTED')"
    )
    op.execute(
        "UPDATE executions SET status = 'FAILED' "
        "WHERE status IN ('WAITING_FOR_APPROVAL', 'REJECTED')"
    )
    op.execute(
        "UPDATE tool_calls SET status = 'FAILED' "
        "WHERE status IN ('WAITING_FOR_APPROVAL', 'REJECTED')"
    )
    op.drop_constraint("tool_call_status", "tool_calls", type_="check")
    op.alter_column(
        "tool_calls",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "tool_call_status",
        "tool_calls",
        "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
    )
    op.drop_constraint("execution_status", "executions", type_="check")
    op.alter_column(
        "executions",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "execution_status",
        "executions",
        "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
    )
    op.drop_constraint("task_status", "tasks", type_="check")
    op.alter_column(
        "tasks",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "task_status",
        "tasks",
        "status IN ('PENDING', 'PLANNING', 'EXECUTING', 'SUCCEEDED', 'FAILED')",
    )
