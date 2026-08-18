"""import receipts

Revision ID: 202607170016
Revises: 202607170015
Create Date: 2026-08-18 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170016"
down_revision: str | None = "202607170015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("invoice_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("vendor_bill_id", sa.Integer(), nullable=True),
        sa.Column("review_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("company_id > 0", name="ck_import_receipts_company_id_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_import_receipts_company_idempotency_key",
        ),
    )
    op.create_index("ix_import_receipts_idempotency_key", "import_receipts", ["idempotency_key"])
    op.create_index("ix_import_receipts_company_status", "import_receipts", ["company_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_import_receipts_company_status", table_name="import_receipts")
    op.drop_index("ix_import_receipts_idempotency_key", table_name="import_receipts")
    op.drop_table("import_receipts")
