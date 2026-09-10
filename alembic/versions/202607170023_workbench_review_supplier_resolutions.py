"""workbench review supplier resolutions

Revision ID: 202607170023
Revises: 202607170022
Create Date: 2026-09-10 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170023"
down_revision: str | None = "202607170022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_supplier_resolutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("source_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("resolved_partner_id", sa.Integer(), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("note", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_supplier_resolutions_company_id_positive",
        ),
        sa.CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_supplier_resolutions_review_version_positive",
        ),
        sa.CheckConstraint(
            "(mode = 'match_existing' AND resolved_partner_id IS NOT NULL) "
            "OR (mode <> 'match_existing' AND resolved_partner_id IS NULL)",
            name="ck_workbench_review_supplier_resolutions_partner_id_by_mode",
        ),
        sa.CheckConstraint(
            "resolved_partner_id IS NULL OR resolved_partner_id > 0",
            name="ck_workbench_review_supplier_resolutions_partner_id_positive",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_supplier_resolutions_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "review_version",
            name="uq_workbench_review_supplier_resolutions_review_version",
        ),
    )
    op.create_index(
        "ix_workbench_review_supplier_resolutions_company_review",
        "workbench_review_supplier_resolutions",
        ["company_id", "review_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workbench_review_supplier_resolutions_company_review",
        table_name="workbench_review_supplier_resolutions",
    )
    op.drop_table("workbench_review_supplier_resolutions")
