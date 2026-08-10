"""execution source invoice evidence

Revision ID: 202607170009
Revises: 202607170008
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170009"
down_revision: str | None = "202607170008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_source_invoice_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.String(length=255), nullable=False),
        sa.Column("source_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("invoice", sa.JSON(), nullable=False),
        sa.Column("partner_match", sa.JSON(), nullable=False),
        sa.Column("product_match", sa.JSON(), nullable=False),
        sa.Column("tax_match", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("company_id > 0", name="ck_execution_source_invoice_evidence_company_id_positive"),
        sa.CheckConstraint(
            "decision_version > 0",
            name="ck_execution_source_invoice_evidence_decision_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["workbench_review_decisions.decision_id"],
            name="fk_execution_source_invoice_evidence_decision_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_source_invoice_evidence_company_review_version",
        "execution_source_invoice_evidence",
        ["company_id", "review_id", "decision_version"],
    )
    op.create_index(
        "ix_execution_source_invoice_evidence_decision_id",
        "execution_source_invoice_evidence",
        ["decision_id"],
    )
    op.create_index(
        "ix_execution_source_invoice_evidence_source_invoice_id",
        "execution_source_invoice_evidence",
        ["source_invoice_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_source_invoice_evidence_source_invoice_id",
        table_name="execution_source_invoice_evidence",
    )
    op.drop_index(
        "ix_execution_source_invoice_evidence_decision_id",
        table_name="execution_source_invoice_evidence",
    )
    op.drop_index(
        "ix_execution_source_invoice_evidence_company_review_version",
        table_name="execution_source_invoice_evidence",
    )
    op.drop_table("execution_source_invoice_evidence")
