"""workbench review decisions

Revision ID: 202607170006
Revises: 202607170005
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170006"
down_revision: str | None = "202607170005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("decision_id", sa.String(length=255), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version_before", sa.Integer(), nullable=False),
        sa.Column("review_version_after", sa.Integer(), nullable=False),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("selected_workflow", sa.String(length=64), nullable=True),
        sa.Column("selected_partner_id", sa.Integer(), nullable=True),
        sa.Column("line_resolutions", sa.JSON(), nullable=False),
        sa.Column("tax_resolutions", sa.JSON(), nullable=False),
        sa.Column("business_context", sa.JSON(), nullable=True),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "review_version_before > 0",
            name="ck_workbench_review_decisions_version_before_positive",
        ),
        sa.CheckConstraint(
            "review_version_after > 0",
            name="ck_workbench_review_decisions_version_after_positive",
        ),
        sa.CheckConstraint(
            "review_version_after = review_version_before + 1",
            name="ck_workbench_review_decisions_version_increment",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_decisions_review_id",
        ),
        sa.UniqueConstraint("decision_id", name="uq_workbench_review_decisions_decision_id"),
        sa.UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_workbench_review_decisions_company_idempotency_key",
        ),
    )
    op.create_index("ix_workbench_review_decisions_company_id", "workbench_review_decisions", ["company_id"])
    op.create_index("ix_workbench_review_decisions_review_id", "workbench_review_decisions", ["review_id"])
    op.create_index("ix_workbench_review_decisions_decision_type", "workbench_review_decisions", ["decision_type"])
    op.create_index("ix_workbench_review_decisions_submitted_at", "workbench_review_decisions", ["submitted_at"])
    op.create_index(
        "ix_workbench_review_decisions_company_review",
        "workbench_review_decisions",
        ["company_id", "review_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workbench_review_decisions_company_review", table_name="workbench_review_decisions")
    op.drop_index("ix_workbench_review_decisions_submitted_at", table_name="workbench_review_decisions")
    op.drop_index("ix_workbench_review_decisions_decision_type", table_name="workbench_review_decisions")
    op.drop_index("ix_workbench_review_decisions_review_id", table_name="workbench_review_decisions")
    op.drop_index("ix_workbench_review_decisions_company_id", table_name="workbench_review_decisions")
    op.drop_table("workbench_review_decisions")
