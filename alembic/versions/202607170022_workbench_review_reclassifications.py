"""workbench review reclassifications

Revision ID: 202607170022
Revises: 202607170021
Create Date: 2026-09-09 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170022"
down_revision: str | None = "202607170021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_reclassifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("source_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=1024), nullable=True),
        sa.Column("previous_workflow", sa.String(length=64), nullable=False),
        sa.Column("previous_review_reasons", sa.JSON(), nullable=False),
        sa.Column("new_workflow", sa.String(length=64), nullable=False),
        sa.Column("new_review_reasons", sa.JSON(), nullable=False),
        sa.Column("matched_rule_code", sa.String(length=120), nullable=True),
        sa.Column("matched_rule_id", sa.String(length=120), nullable=True),
        sa.Column("executable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_reclassifications_company_id_positive",
        ),
        sa.CheckConstraint(
            "from_version > 0",
            name="ck_workbench_review_reclassifications_from_version_positive",
        ),
        sa.CheckConstraint(
            "to_version > 0",
            name="ck_workbench_review_reclassifications_to_version_positive",
        ),
        sa.CheckConstraint(
            "to_version = from_version + 1",
            name="ck_workbench_review_reclassifications_single_step",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_reclassifications_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "to_version",
            name="uq_workbench_review_reclassifications_review_to_version",
        ),
        sa.UniqueConstraint(
            "review_id",
            "from_version",
            name="uq_workbench_review_reclassifications_review_from_version",
        ),
    )
    op.create_index(
        "ix_workbench_review_reclassifications_company_review",
        "workbench_review_reclassifications",
        ["company_id", "review_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workbench_review_reclassifications_company_review",
        table_name="workbench_review_reclassifications",
    )
    op.drop_table("workbench_review_reclassifications")
