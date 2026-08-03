"""workbench review items

Revision ID: 202607170005
Revises: 202607170004
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170005"
down_revision: str | None = "202607170004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.String(length=255), nullable=False),
        sa.Column("invoice_number", sa.String(length=255), nullable=True),
        sa.Column("supplier_tax_number", sa.String(length=64), nullable=True),
        sa.Column("supplier_name", sa.String(length=512), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("total_amount", sa.Numeric(24, 6), nullable=True),
        sa.Column("workflow", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_reasons", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.UniqueConstraint("review_id", name="uq_workbench_review_items_review_id"),
        sa.UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_workbench_review_items_company_idempotency_key",
        ),
    )
    op.create_index("ix_workbench_review_items_company_id", "workbench_review_items", ["company_id"])
    op.create_index("ix_workbench_review_items_status", "workbench_review_items", ["status"])
    op.create_index("ix_workbench_review_items_created_at", "workbench_review_items", ["created_at"])
    op.create_index(
        "ix_workbench_review_items_supplier_tax_number",
        "workbench_review_items",
        ["supplier_tax_number"],
    )
    op.create_index(
        "ix_workbench_review_items_company_status_created",
        "workbench_review_items",
        ["company_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workbench_review_items_company_status_created", table_name="workbench_review_items")
    op.drop_index("ix_workbench_review_items_supplier_tax_number", table_name="workbench_review_items")
    op.drop_index("ix_workbench_review_items_created_at", table_name="workbench_review_items")
    op.drop_index("ix_workbench_review_items_status", table_name="workbench_review_items")
    op.drop_index("ix_workbench_review_items_company_id", table_name="workbench_review_items")
    op.drop_table("workbench_review_items")
