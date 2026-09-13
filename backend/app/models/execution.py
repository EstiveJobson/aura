from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constraints import PLANNER_NAME_MAX_LENGTH, TOOL_NAME_MAX_LENGTH
from app.database.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    OUTCOME_UNCERTAIN = "outcome_uncertain"


class ExecutionStatus(StrEnum):
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    OUTCOME_UNCERTAIN = "outcome_uncertain"


class ToolCallStatus(StrEnum):
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    OUTCOME_UNCERTAIN = "outcome_uncertain"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", native_enum=False, create_constraint=True),
        default=TaskStatus.PENDING,
        index=True,
    )
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    plan: Mapped[Plan | None] = relationship(
        back_populates="task", cascade="all, delete-orphan", uselist=False
    )
    execution: Mapped[Execution | None] = relationship(
        back_populates="task", cascade="all, delete-orphan", uselist=False
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True)
    planner: Mapped[str] = mapped_column(String(PLANNER_NAME_MAX_LENGTH))
    summary: Mapped[str] = mapped_column(Text)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    task: Mapped[Task] = relationship(back_populates="plan")


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True)
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(
            ExecutionStatus,
            name="execution_status",
            native_enum=False,
            create_constraint=True,
        ),
        default=ExecutionStatus.RUNNING,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[Task] = relationship(back_populates="execution")
    tool_calls: Mapped[list[ToolCall]] = relationship(
        back_populates="execution", cascade="all, delete-orphan", order_by="ToolCall.started_at"
    )
    approval: Mapped[Approval | None] = relationship(
        back_populates="execution", cascade="all, delete-orphan", uselist=False
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(TOOL_NAME_MAX_LENGTH))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[ToolCallStatus] = mapped_column(
        Enum(
            ToolCallStatus,
            name="tool_call_status",
            native_enum=False,
            create_constraint=True,
        ),
        default=ToolCallStatus.RUNNING,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    execution: Mapped[Execution] = relationship(back_populates="tool_calls")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), unique=True
    )
    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(
            ApprovalDecision,
            name="approval_decision",
            native_enum=False,
            create_constraint=True,
        ),
        default=ApprovalDecision.PENDING,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filesystem_preconditions: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    execution: Mapped[Execution] = relationship(back_populates="approval")
