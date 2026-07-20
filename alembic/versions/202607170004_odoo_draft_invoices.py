"""odoo draft invoices

Revision ID: 202607170004
Revises: 202607170003
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170004"
down_revision: str | None = "202607170003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "odoo_draft_invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("integration_invoice_id", sa.Integer(), nullable=True),
        sa.Column("ettn", sa.String(length=255), nullable=False),
        sa.Column("invoice_number", sa.String(length=255), nullable=True),
        sa.Column("odoo_model", sa.String(length=64), nullable=False),
        sa.Column("odoo_move_id", sa.Integer(), nullable=True),
        sa.Column("creation_status", sa.String(length=32), nullable=False),
        sa.Column("safe_error_category", sa.String(length=64), nullable=True),
        sa.Column("safe_error_message", sa.String(length=512), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("odoo_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("ettn", name="uq_odoo_draft_invoice_ettn"),
    )
    op.create_index(
        "ix_odoo_draft_invoices_odoo_move_id",
        "odoo_draft_invoices",
        ["odoo_move_id"],
    )
    op.create_index(
        "ix_odoo_draft_invoices_creation_status",
        "odoo_draft_invoices",
        ["creation_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_odoo_draft_invoices_creation_status", table_name="odoo_draft_invoices")
    op.drop_index("ix_odoo_draft_invoices_odoo_move_id", table_name="odoo_draft_invoices")
    op.drop_table("odoo_draft_invoices")
