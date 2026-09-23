"""review scoped purchase purpose and accounting resolution

Revision ID: 202607170031
Revises: 202607170030
Create Date: 2026-09-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170031"
down_revision: str | None = "202607170030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_purchase_purpose_resolutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("source_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("purchase_purpose", sa.String(length=32), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("note", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_purchase_purpose_resolutions_company_id_positive",
        ),
        sa.CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_purchase_purpose_resolutions_review_version_positive",
        ),
        sa.CheckConstraint(
            "purchase_purpose IN ('internal_use', 'resale', 'customer_project', 'other_operating_expense')",
            name="ck_workbench_review_purchase_purpose_resolutions_purpose",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_purchase_purpose_resolutions_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "review_version",
            name="uq_workbench_review_purchase_purpose_resolutions_review_version",
        ),
    )
    op.create_index(
        "ix_workbench_review_purchase_purpose_resolutions_company_review",
        "workbench_review_purchase_purpose_resolutions",
        ["company_id", "review_id"],
    )

    op.create_table(
        "workbench_review_accounting_resolutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("treatment_type", sa.String(length=32), nullable=False),
        sa.Column("expense_account_id", sa.Integer(), nullable=False),
        sa.Column("expense_category", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("note", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_accounting_resolutions_company_id_positive",
        ),
        sa.CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_accounting_resolutions_review_version_positive",
        ),
        sa.CheckConstraint(
            "treatment_type IN ('expense_account')",
            name="ck_workbench_review_accounting_resolutions_treatment_type",
        ),
        sa.CheckConstraint(
            "expense_account_id > 0",
            name="ck_workbench_review_accounting_resolutions_expense_account_id_positive",
        ),
        sa.CheckConstraint(
            "length(trim(expense_category)) > 0",
            name="ck_workbench_review_accounting_resolutions_expense_category_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_accounting_resolutions_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "review_version",
            name="uq_workbench_review_accounting_resolutions_review_version",
        ),
    )
    op.create_index(
        "ix_workbench_review_accounting_resolutions_company_review",
        "workbench_review_accounting_resolutions",
        ["company_id", "review_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workbench_review_accounting_resolutions_company_review",
        table_name="workbench_review_accounting_resolutions",
    )
    op.drop_table("workbench_review_accounting_resolutions")
    op.drop_index(
        "ix_workbench_review_purchase_purpose_resolutions_company_review",
        table_name="workbench_review_purchase_purpose_resolutions",
    )
    op.drop_table("workbench_review_purchase_purpose_resolutions")
