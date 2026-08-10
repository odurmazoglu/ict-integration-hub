"""workbench review execution evidence

Revision ID: 202607170011
Revises: 202607170009
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170011"
down_revision: str | None = "202607170009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_execution_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("source_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("invoice", sa.JSON(), nullable=False),
        sa.Column("partner_match", sa.JSON(), nullable=False),
        sa.Column("product_match", sa.JSON(), nullable=False),
        sa.Column("tax_match", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_execution_evidence_company_id_positive",
        ),
        sa.CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_execution_evidence_review_version_positive",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_workbench_review_execution_evidence_schema_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_execution_evidence_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "review_id",
            "review_version",
            name="uq_workbench_review_execution_evidence_company_review_version",
        ),
    )
    op.create_index(
        "ix_workbench_review_execution_evidence_company_review_version",
        "workbench_review_execution_evidence",
        ["company_id", "review_id", "review_version"],
    )
    op.create_index(
        "ix_workbench_review_execution_evidence_source_invoice_id",
        "workbench_review_execution_evidence",
        ["source_invoice_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workbench_review_execution_evidence_source_invoice_id",
        table_name="workbench_review_execution_evidence",
    )
    op.drop_index(
        "ix_workbench_review_execution_evidence_company_review_version",
        table_name="workbench_review_execution_evidence",
    )
    op.drop_table("workbench_review_execution_evidence")
