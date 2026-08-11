"""workbench review billing evidence

Revision ID: 202607170013
Revises: 202607170012
Create Date: 2026-08-11 10:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170013"
down_revision: str | None = "202607170012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_billing_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("billing_key", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("billing_instruction", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_billing_evidence_company_id_positive",
        ),
        sa.CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_billing_evidence_review_version_positive",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_workbench_review_billing_evidence_schema_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_billing_evidence_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "review_id",
            "review_version",
            "billing_key",
            name="uq_workbench_review_billing_evidence_company_review_version_key",
        ),
    )
    op.create_index(
        "ix_workbench_review_billing_evidence_company_review_version",
        "workbench_review_billing_evidence",
        ["company_id", "review_id", "review_version"],
    )
    op.create_index(
        "ix_workbench_review_billing_evidence_billing_key",
        "workbench_review_billing_evidence",
        ["billing_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workbench_review_billing_evidence_billing_key",
        table_name="workbench_review_billing_evidence",
    )
    op.drop_index(
        "ix_workbench_review_billing_evidence_company_review_version",
        table_name="workbench_review_billing_evidence",
    )
    op.drop_table("workbench_review_billing_evidence")
