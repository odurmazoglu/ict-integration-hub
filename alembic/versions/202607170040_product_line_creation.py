"""PR D: Workbench new-product creation (ledger action + creation state table)

Revision ID: 202607170040
Revises: 202607170039
Create Date: 2026-10-12 12:00:00.000000

* Widens ck_workbench_operator_requests_action to allow ``product_line_create``: a new
  product requested on one Workbench child product line row (``odoo_record_id`` is the
  child row id).
* Creates ``workbench_product_line_creations``: one row per review line/version with the
  immutable operator intent, the Hub-derived supplier identity, the crash-recovery status
  and the created Odoo ids.

No existing row, column, index or constraint is otherwise changed.

Downgrade refuses (nothing is deleted or changed) while any ``product_line_create``
ledger row or any ``workbench_product_line_creations`` row exists: both are audit and
idempotency history of Odoo master-data writes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170040"
down_revision: str | None = "202607170039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER = "workbench_operator_requests"
_ACTION_CK = "ck_workbench_operator_requests_action"
_OLD_ACTIONS = (
    "action IN ('supplier_resolution', 'purchase_purpose', 'accounting_resolution', 'decision', "
    "'execute_vendor_bill', 'product_mapping', 'product_line_mapping')"
)
_NEW_ACTIONS = (
    "action IN ('supplier_resolution', 'purchase_purpose', 'accounting_resolution', 'decision', "
    "'execute_vendor_bill', 'product_mapping', 'product_line_mapping', 'product_line_create')"
)
_TABLE = "workbench_product_line_creations"


def upgrade() -> None:
    with op.batch_alter_table(_LEDGER) as batch_op:
        batch_op.drop_constraint(_ACTION_CK, type_="check")
        batch_op.create_check_constraint(_ACTION_CK, _NEW_ACTIONS)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("supplier_partner_id", sa.Integer(), nullable=False),
        sa.Column("seller_item_code", sa.String(length=255), nullable=False),
        sa.Column("product_name", sa.String(length=255), nullable=False),
        sa.Column("product_kind", sa.String(length=32), nullable=False),
        sa.Column("categ_id", sa.Integer(), nullable=False),
        sa.Column("uom_id", sa.Integer(), nullable=False),
        sa.Column("internal_reference", sa.String(length=64), nullable=True),
        sa.Column("intent_digest", sa.String(length=64), nullable=False),
        sa.Column("supplier_product_name", sa.String(length=512), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("create_attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("product_template_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("supplierinfo_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["workbench_review_items.review_id"], name="fk_wplc_review_id"),
        sa.CheckConstraint("company_id > 0", name="ck_wplc_company_positive"),
        sa.CheckConstraint("review_version > 0", name="ck_wplc_version_positive"),
        sa.CheckConstraint("supplier_partner_id > 0", name="ck_wplc_partner_positive"),
        sa.CheckConstraint("categ_id > 0", name="ck_wplc_categ_positive"),
        sa.CheckConstraint("uom_id > 0", name="ck_wplc_uom_positive"),
        sa.CheckConstraint(
            "status IN ('create_attempted', 'product_created', 'completed', 'needs_reconciliation')",
            name="ck_wplc_status",
        ),
        sa.CheckConstraint("product_kind IN ('service', 'goods', 'stocked_goods')", name="ck_wplc_product_kind"),
        sa.CheckConstraint(
            "status NOT IN ('product_created', 'completed') "
            "OR (product_template_id IS NOT NULL AND product_id IS NOT NULL)",
            name="ck_wplc_product_by_status",
        ),
        sa.CheckConstraint(
            "status != 'completed' OR supplierinfo_id IS NOT NULL", name="ck_wplc_supplierinfo_by_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_id", "company_id", "review_version", "line_number", name="uq_wplc_line"),
    )
    op.create_index("ix_wplc_company_review", _TABLE, ["company_id", "review_id"])


def downgrade() -> None:
    bind = op.get_bind()
    ledger_rows = bind.scalar(sa.text(f"SELECT count(*) FROM {_LEDGER} WHERE action = 'product_line_create'"))
    creation_rows = bind.scalar(sa.text(f"SELECT count(*) FROM {_TABLE}"))
    if ledger_rows or creation_rows:
        raise RuntimeError(
            f"Refusing to downgrade 202607170040: {ledger_rows} 'product_line_create' ledger row(s) and "
            f"{creation_rows} {_TABLE} row(s) exist. They are audit/idempotency history of Odoo product "
            "creation and are not deleted automatically; resolve them explicitly before downgrading."
        )
    op.drop_index("ix_wplc_company_review", table_name=_TABLE)
    op.drop_table(_TABLE)
    with op.batch_alter_table(_LEDGER) as batch_op:
        batch_op.drop_constraint(_ACTION_CK, type_="check")
        batch_op.create_check_constraint(_ACTION_CK, _OLD_ACTIONS)
