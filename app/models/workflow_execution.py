from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_workflow_executions_execution_id"),
        UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_workflow_executions_company_idempotency_key",
        ),
        CheckConstraint("company_id > 0", name="ck_workflow_executions_company_id_positive"),
        CheckConstraint("decision_version > 0", name="ck_workflow_executions_decision_version_positive"),
        Index("ix_workflow_executions_review_id", "review_id"),
        Index("ix_workflow_executions_company_id", "company_id"),
        Index("ix_workflow_executions_state", "state"),
        Index("ix_workflow_executions_company_review", "company_id", "review_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(255), nullable=False)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    decision_version: Mapped[int] = mapped_column(nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_signature: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    current_step_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    runtime_version: Mapped[int] = mapped_column(nullable=False, default=1)
    next_event_sequence: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    steps: Mapped[list[WorkflowExecutionStep]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="WorkflowExecutionStep.sequence",
    )
    events: Mapped[list[WorkflowExecutionEvent]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="WorkflowExecutionEvent.sequence",
    )


class WorkflowExecutionStep(Base):
    __tablename__ = "workflow_execution_steps"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "step_key",
            name="uq_workflow_execution_steps_execution_step_key",
        ),
        UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_workflow_execution_steps_execution_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_workflow_execution_steps_sequence_positive"),
        CheckConstraint("retry_count >= 0", name="ck_workflow_execution_steps_retry_count_nonnegative"),
        Index("ix_workflow_execution_steps_execution_id", "execution_id"),
        Index("ix_workflow_execution_steps_state", "state"),
        Index("ix_workflow_execution_steps_step_type", "step_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("workflow_executions.execution_id"),
        nullable=False,
    )
    step_key: Mapped[str] = mapped_column(String(512), nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    allocation_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    retry_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    execution: Mapped[WorkflowExecution] = relationship(back_populates="steps")


class WorkflowExecutionEvent(Base):
    __tablename__ = "workflow_execution_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_workflow_execution_events_event_id"),
        UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_workflow_execution_events_execution_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_workflow_execution_events_sequence_positive"),
        Index("ix_workflow_execution_events_execution_id", "execution_id"),
        Index("ix_workflow_execution_events_event_type", "event_type"),
        Index("ix_workflow_execution_events_occurred_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("workflow_executions.execution_id"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    step_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    step_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)

    execution: Mapped[WorkflowExecution] = relationship(back_populates="events")
