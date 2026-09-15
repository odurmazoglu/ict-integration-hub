"""workbench review product remediation reservations and identity claims

Revision ID: 202607170026
Revises: 202607170025
Create Date: 2026-09-15 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170026"
down_revision: str | None = "202607170025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_product_remediation_reservations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resolved_supplier_partner_id", sa.Integer(), nullable=False),
        sa.Column("seller_item_code", sa.String(length=255), nullable=False),
        sa.Column("product_name", sa.String(length=512), nullable=False),
        sa.Column("is_storable", sa.Boolean(), nullable=False),
        sa.Column("internal_reference", sa.String(length=255), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("note", sa.String(length=1024), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("product_template_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("supplierinfo_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_product_remediation_reservations_company_id_positive",
        ),
        sa.CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_product_remediation_reservations_review_version_positive",
        ),
        sa.CheckConstraint(
            "resolved_supplier_partner_id > 0",
            name="ck_workbench_review_product_remediation_reservations_partner_id_positive",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'create_attempted', 'product_created', 'completed', "
            "'reused_existing_product', 'needs_reconciliation')",
            name="ck_workbench_review_product_remediation_reservations_status",
        ),
        sa.CheckConstraint(
            "product_template_id IS NULL OR product_template_id > 0",
            name="ck_workbench_review_product_remediation_reservations_template_id_positive",
        ),
        sa.CheckConstraint(
            "product_id IS NULL OR product_id > 0",
            name="ck_workbench_review_product_remediation_reservations_product_id_positive",
        ),
        sa.CheckConstraint(
            "supplierinfo_id IS NULL OR supplierinfo_id > 0",
            name="ck_workbench_review_product_remediation_reservations_supplierinfo_id_positive",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'create_attempted', 'needs_reconciliation') OR product_template_id IS NOT NULL",
            name="ck_workbench_review_product_remediation_reservations_template_by_status",
        ),
        sa.CheckConstraint(
            "status != 'completed' OR supplierinfo_id IS NOT NULL",
            name="ck_workbench_review_product_remediation_reservations_supplierinfo_by_status",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_product_remediation_reservations_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "company_id",
            "review_version",
            "line_number",
            name="uq_workbench_review_product_remediation_reservations_line",
        ),
    )
    op.create_index(
        "ix_workbench_review_product_remediation_reservations_company_review",
        "workbench_review_product_remediation_reservations",
        ["company_id", "review_id"],
    )

    op.create_table(
        "workbench_review_product_identity_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("resolved_supplier_partner_id", sa.Integer(), nullable=False),
        sa.Column("seller_item_code", sa.String(length=255), nullable=False),
        sa.Column("owner_review_id", sa.String(length=255), nullable=False),
        sa.Column("owner_company_id", sa.Integer(), nullable=False),
        sa.Column("owner_review_version", sa.Integer(), nullable=False),
        sa.Column("owner_line_number", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_product_identity_claims_company_id_positive",
        ),
        sa.CheckConstraint(
            "resolved_supplier_partner_id > 0",
            name="ck_workbench_review_product_identity_claims_partner_id_positive",
        ),
        sa.CheckConstraint(
            "owner_company_id > 0",
            name="ck_workbench_review_product_identity_claims_owner_company_id_positive",
        ),
        sa.CheckConstraint(
            "owner_review_version > 0",
            name="ck_workbench_review_product_identity_claims_owner_review_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["owner_review_id", "owner_company_id", "owner_review_version", "owner_line_number"],
            [
                "workbench_review_product_remediation_reservations.review_id",
                "workbench_review_product_remediation_reservations.company_id",
                "workbench_review_product_remediation_reservations.review_version",
                "workbench_review_product_remediation_reservations.line_number",
            ],
            name="fk_workbench_review_product_identity_claims_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "resolved_supplier_partner_id",
            "seller_item_code",
            name="uq_workbench_review_product_identity_claims_identity",
        ),
    )


def downgrade() -> None:
    op.drop_table("workbench_review_product_identity_claims")
    op.drop_index(
        "ix_workbench_review_product_remediation_reservations_company_review",
        table_name="workbench_review_product_remediation_reservations",
    )
    op.drop_table("workbench_review_product_remediation_reservations")
