"""Represent uncertain Phase 3.2 write outcomes.

Revision ID: 20260913_0004
Revises: 20260912_0003
Create Date: 2026-09-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260913_0004"
down_revision: str | None = "20260912_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("task_status", "tasks", type_="check")
    op.create_check_constraint(
        "task_status",
        "tasks",
        "status IN ('PENDING', 'PLANNING', 'WAITING_FOR_APPROVAL', "
        "'EXECUTING', 'SUCCEEDED', 'REJECTED', 'FAILED', 'OUTCOME_UNCERTAIN')",
    )
    op.drop_constraint("execution_status", "executions", type_="check")
    op.create_check_constraint(
        "execution_status",
        "executions",
        "status IN ('WAITING_FOR_APPROVAL', 'RUNNING', 'SUCCEEDED', 'REJECTED', "
        "'FAILED', 'OUTCOME_UNCERTAIN')",
    )
    op.drop_constraint("tool_call_status", "tool_calls", type_="check")
    op.create_check_constraint(
        "tool_call_status",
        "tool_calls",
        "status IN ('WAITING_FOR_APPROVAL', 'RUNNING', 'SUCCEEDED', 'REJECTED', "
        "'FAILED', 'OUTCOME_UNCERTAIN')",
    )


def downgrade() -> None:
    op.execute("UPDATE tasks SET status = 'FAILED' WHERE status = 'OUTCOME_UNCERTAIN'")
    op.execute("UPDATE executions SET status = 'FAILED' WHERE status = 'OUTCOME_UNCERTAIN'")
    op.execute("UPDATE tool_calls SET status = 'FAILED' WHERE status = 'OUTCOME_UNCERTAIN'")

    op.drop_constraint("tool_call_status", "tool_calls", type_="check")
    op.create_check_constraint(
        "tool_call_status",
        "tool_calls",
        "status IN ('WAITING_FOR_APPROVAL', 'RUNNING', 'SUCCEEDED', 'REJECTED', 'FAILED')",
    )
    op.drop_constraint("execution_status", "executions", type_="check")
    op.create_check_constraint(
        "execution_status",
        "executions",
        "status IN ('WAITING_FOR_APPROVAL', 'RUNNING', 'SUCCEEDED', 'REJECTED', 'FAILED')",
    )
    op.drop_constraint("task_status", "tasks", type_="check")
    op.create_check_constraint(
        "task_status",
        "tasks",
        "status IN ('PENDING', 'PLANNING', 'WAITING_FOR_APPROVAL', "
        "'EXECUTING', 'SUCCEEDED', 'REJECTED', 'FAILED')",
    )
