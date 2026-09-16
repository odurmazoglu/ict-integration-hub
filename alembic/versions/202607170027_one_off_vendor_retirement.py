"""one-off vendor retirement lifecycle (P0-PROD-08H)

Revision ID: 202607170027
Revises: 202607170026
Create Date: 2026-09-16 14:00:00.000000

New table for the ONE_OFF_VENDOR archive-last durable state machine, plus a
required alteration to the existing supplier remediation effects table's mode
CHECK constraint (it previously hard-coded an explicit allowlist of exactly
two mode values; a third, `one_off_vendor`, must now also be representable).

Constraint/index names on the new table use the abbreviated "wrov_retirements"
prefix and are kept under PostgreSQL's 63-byte NAMEDATALEN limit -- see the
identical convention (and the incident it prevents) on
`workbench_review_product_remediation_reservations` (revision 202607170026).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170027"
down_revision: str | None = "202607170026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUS_BY_MODE_CHECK = (
    "(mode = 'create_permanent_supplier' AND partner_write_status IN ('created', 'already_exists')) "
    "OR (mode = 'match_existing' AND partner_write_status = 'selected')"
)
_NEW_STATUS_BY_MODE_CHECK = (
    "(mode = 'create_permanent_supplier' AND partner_write_status IN ('created', 'already_exists')) "
    "OR (mode = 'match_existing' AND partner_write_status = 'selected') "
    "OR (mode = 'one_off_vendor' AND partner_write_status IN ('created', 'already_exists'))"
)


def upgrade() -> None:
    # batch_alter_table: SQLite has no ALTER-constraint support and requires the
    # copy-and-move strategy; on PostgreSQL this still emits plain ALTER TABLE.
    with op.batch_alter_table("workbench_review_supplier_remediation_effects") as batch_op:
        batch_op.drop_constraint(
            "ck_workbench_review_supplier_remediation_effects_status_by_mode",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_workbench_review_supplier_remediation_effects_status_by_mode",
            _NEW_STATUS_BY_MODE_CHECK,
        )

    op.create_table(
        "workbench_review_one_off_vendor_retirements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("resolved_partner_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wrov_retirements_review_id",
        ),
        sa.CheckConstraint("company_id > 0", name="ck_wrov_retirements_company_positive"),
        sa.CheckConstraint("review_version > 0", name="ck_wrov_retirements_version_positive"),
        sa.CheckConstraint("resolved_partner_id > 0", name="ck_wrov_retirements_partner_positive"),
        sa.CheckConstraint(
            "status IN ('pending_vendor_bill', 'archive_attempted', 'archived', 'needs_reconciliation')",
            name="ck_wrov_retirements_status",
        ),
        sa.UniqueConstraint(
            "review_id",
            "company_id",
            "review_version",
            name="uq_wrov_retirements_review_version",
        ),
    )
    op.create_index(
        "ix_wrov_retirements_company_review",
        "workbench_review_one_off_vendor_retirements",
        ["company_id", "review_id"],
    )
    op.create_index(
        "ix_wrov_retirements_partner_id",
        "workbench_review_one_off_vendor_retirements",
        ["resolved_partner_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wrov_retirements_partner_id",
        table_name="workbench_review_one_off_vendor_retirements",
    )
    op.drop_index(
        "ix_wrov_retirements_company_review",
        table_name="workbench_review_one_off_vendor_retirements",
    )
    op.drop_table("workbench_review_one_off_vendor_retirements")

    with op.batch_alter_table("workbench_review_supplier_remediation_effects") as batch_op:
        batch_op.drop_constraint(
            "ck_workbench_review_supplier_remediation_effects_status_by_mode",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_workbench_review_supplier_remediation_effects_status_by_mode",
            _OLD_STATUS_BY_MODE_CHECK,
        )
