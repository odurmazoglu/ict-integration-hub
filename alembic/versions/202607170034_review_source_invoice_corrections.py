"""append-only review source-invoice corrections (P0-PROD historical supplier tax id)

Revision ID: 202607170034
Revises: 202607170033
Create Date: 2026-10-01 12:00:00.000000

Additive only: creates ``workbench_review_source_invoice_corrections``. No existing
row of any table is read, rewritten or backfilled -- the original
``workbench_review_source_invoice_evidence`` rows stay byte-identical, and a
correction only ever enters through the audited correction use case.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170034"
down_revision: str | None = "202607170033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workbench_review_source_invoice_corrections"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("source_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("field_path", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.String(length=255), nullable=True),
        sa.Column("new_value", sa.String(length=255), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("source_document_sha256", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_wr_source_corrections_company_id_positive",
        ),
        sa.CheckConstraint(
            "from_version > 0",
            name="ck_wr_source_corrections_from_version_positive",
        ),
        sa.CheckConstraint(
            "to_version = from_version + 1",
            name="ck_wr_source_corrections_single_step",
        ),
        sa.CheckConstraint(
            "field_path IN ('supplier.tax_number')",
            name="ck_wr_source_corrections_field_path",
        ),
        sa.CheckConstraint(
            "reason IN ('UBL_PARTY_TAX_IDENTIFIER_PR201')",
            name="ck_wr_source_corrections_reason",
        ),
        sa.CheckConstraint(
            "old_value IS NULL OR old_value <> new_value",
            name="ck_wr_source_corrections_value_changes",
        ),
        sa.CheckConstraint(
            "source_document_id > 0",
            name="ck_wr_source_corrections_document_id_positive",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wr_source_corrections_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "to_version",
            name="uq_wr_source_corrections_review_to_version",
        ),
        sa.UniqueConstraint(
            "review_id",
            "from_version",
            name="uq_wr_source_corrections_review_from_version",
        ),
    )
    op.create_index(
        "ix_wr_source_corrections_company_review",
        _TABLE,
        ["company_id", "review_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_wr_source_corrections_company_review", table_name=_TABLE)
    op.drop_table(_TABLE)
