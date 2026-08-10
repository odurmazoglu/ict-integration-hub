"""workflow execution runtime

Revision ID: 202607170008
Revises: 202607170007
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170008"
down_revision: str | None = "202607170007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.String(length=255), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("plan_signature", sa.String(length=255), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("failure", sa.JSON(), nullable=True),
        sa.Column("current_step_key", sa.String(length=512), nullable=True),
        sa.Column("runtime_version", sa.Integer(), nullable=False),
        sa.Column("next_event_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("company_id > 0", name="ck_workflow_executions_company_id_positive"),
        sa.CheckConstraint("decision_version > 0", name="ck_workflow_executions_decision_version_positive"),
        sa.CheckConstraint("runtime_version > 0", name="ck_workflow_executions_runtime_version_positive"),
        sa.CheckConstraint("next_event_sequence > 0", name="ck_workflow_executions_next_event_sequence_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", name="uq_workflow_executions_execution_id"),
        sa.UniqueConstraint("company_id", "idempotency_key", name="uq_workflow_executions_company_idempotency_key"),
    )
    op.create_index("ix_workflow_executions_company_id", "workflow_executions", ["company_id"])
    op.create_index("ix_workflow_executions_company_review", "workflow_executions", ["company_id", "review_id"])
    op.create_index("ix_workflow_executions_review_id", "workflow_executions", ["review_id"])
    op.create_index("ix_workflow_executions_state", "workflow_executions", ["state"])

    op.create_table(
        "workflow_execution_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.String(length=255), nullable=False),
        sa.Column("step_key", sa.String(length=512), nullable=False),
        sa.Column("step_type", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("allocation_keys", sa.JSON(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_result", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("retry_count >= 0", name="ck_workflow_execution_steps_retry_count_nonnegative"),
        sa.CheckConstraint("sequence > 0", name="ck_workflow_execution_steps_sequence_positive"),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_executions.execution_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_workflow_execution_steps_execution_sequence"),
        sa.UniqueConstraint("execution_id", "step_key", name="uq_workflow_execution_steps_execution_step_key"),
    )
    op.create_index("ix_workflow_execution_steps_execution_id", "workflow_execution_steps", ["execution_id"])
    op.create_index("ix_workflow_execution_steps_state", "workflow_execution_steps", ["state"])
    op.create_index("ix_workflow_execution_steps_step_type", "workflow_execution_steps", ["step_type"])

    op.create_table(
        "workflow_execution_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("execution_id", sa.String(length=255), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("step_key", sa.String(length=512), nullable=True),
        sa.Column("step_type", sa.String(length=64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_workflow_execution_events_sequence_positive"),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_executions.execution_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_workflow_execution_events_event_id"),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_workflow_execution_events_execution_sequence"),
    )
    op.create_index("ix_workflow_execution_events_event_type", "workflow_execution_events", ["event_type"])
    op.create_index("ix_workflow_execution_events_execution_id", "workflow_execution_events", ["execution_id"])
    op.create_index("ix_workflow_execution_events_occurred_at", "workflow_execution_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_execution_events_occurred_at", table_name="workflow_execution_events")
    op.drop_index("ix_workflow_execution_events_execution_id", table_name="workflow_execution_events")
    op.drop_index("ix_workflow_execution_events_event_type", table_name="workflow_execution_events")
    op.drop_table("workflow_execution_events")

    op.drop_index("ix_workflow_execution_steps_step_type", table_name="workflow_execution_steps")
    op.drop_index("ix_workflow_execution_steps_state", table_name="workflow_execution_steps")
    op.drop_index("ix_workflow_execution_steps_execution_id", table_name="workflow_execution_steps")
    op.drop_table("workflow_execution_steps")

    op.drop_index("ix_workflow_executions_state", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_review_id", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_company_review", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_company_id", table_name="workflow_executions")
    op.drop_table("workflow_executions")
