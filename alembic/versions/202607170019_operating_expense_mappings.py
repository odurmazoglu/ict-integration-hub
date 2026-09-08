"""operating expense mappings

Revision ID: 202607170019
Revises: 202607170018
Create Date: 2026-09-08 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170019"
down_revision: str | None = "202607170018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operating_expense_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("vendor_partner_id", sa.Integer(), nullable=False),
        sa.Column("expense_account_id", sa.Integer(), nullable=False),
        sa.Column("expense_category", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("company_id > 0", name="ck_operating_expense_mappings_company_id_positive"),
        sa.CheckConstraint(
            "vendor_partner_id > 0",
            name="ck_operating_expense_mappings_vendor_partner_id_positive",
        ),
        sa.CheckConstraint(
            "expense_account_id > 0",
            name="ck_operating_expense_mappings_expense_account_id_positive",
        ),
        sa.CheckConstraint(
            "length(trim(expense_category)) > 0",
            name="ck_operating_expense_mappings_expense_category_not_empty",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operating_expense_mappings_company_partner",
        "operating_expense_mappings",
        ["company_id", "vendor_partner_id"],
    )
    op.create_index(
        "uq_operating_expense_mappings_active_supplier",
        "operating_expense_mappings",
        ["company_id", "vendor_partner_id"],
        unique=True,
        sqlite_where=sa.text("enabled"),
        postgresql_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_operating_expense_mappings_active_supplier",
        table_name="operating_expense_mappings",
    )
    op.drop_index(
        "ix_operating_expense_mappings_company_partner",
        table_name="operating_expense_mappings",
    )
    op.drop_table("operating_expense_mappings")
