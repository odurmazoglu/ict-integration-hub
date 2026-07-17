"""uyumsoft invoice metadata

Revision ID: 202607170001
Revises: 202607160001
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170001"
down_revision: str | None = "202607160001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uyumsoft_invoice_metadata",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("provider_invoice_id", sa.String(length=255), nullable=True),
        sa.Column("ettn", sa.String(length=255), nullable=True),
        sa.Column("identity_key", sa.String(length=255), nullable=False),
        sa.Column("identity_strategy", sa.String(length=32), nullable=False),
        sa.Column("invoice_number", sa.String(length=255), nullable=True),
        sa.Column("invoice_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sender_name", sa.String(length=512), nullable=True),
        sa.Column("sender_tax_number", sa.String(length=64), nullable=True),
        sa.Column("receiver_name", sa.String(length=512), nullable=True),
        sa.Column("receiver_tax_number", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("provider_status", sa.String(length=128), nullable=True),
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "direction", "ettn", name="uq_uyumsoft_invoice_provider_direction_ettn"),
        sa.UniqueConstraint(
            "provider",
            "direction",
            "identity_key",
            name="uq_uyumsoft_invoice_provider_direction_identity",
        ),
    )
    op.create_index(
        "ix_uyumsoft_invoice_provider_direction",
        "uyumsoft_invoice_metadata",
        ["provider", "direction"],
    )
    op.create_index("ix_uyumsoft_invoice_ettn", "uyumsoft_invoice_metadata", ["ettn"])
    op.create_index("ix_uyumsoft_invoice_invoice_date", "uyumsoft_invoice_metadata", ["invoice_date"])


def downgrade() -> None:
    op.drop_index("ix_uyumsoft_invoice_invoice_date", table_name="uyumsoft_invoice_metadata")
    op.drop_index("ix_uyumsoft_invoice_ettn", table_name="uyumsoft_invoice_metadata")
    op.drop_index("ix_uyumsoft_invoice_provider_direction", table_name="uyumsoft_invoice_metadata")
    op.drop_table("uyumsoft_invoice_metadata")
