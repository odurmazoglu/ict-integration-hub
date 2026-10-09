"""allow the product_line_mapping action in the operator request ledger

Revision ID: 202607170039
Revises: 202607170038
Create Date: 2026-10-09 12:00:00.000000

PR C lets an operator submit the existing-product mapping on one Workbench child
product line row (``x_ipp_wb_product_line``). The ledger records it under its own
action ``product_line_mapping`` so its ``odoo_record_id`` (a child row id) is never
confused with a parent Workbench row id. This widens exactly
ck_workbench_operator_requests_action. No column, index, other constraint or data change.

Downgrade refuses (no row is deleted or changed) while any ledger row has
action = 'product_line_mapping'; those rows are audit/idempotency history.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170039"
down_revision: str | None = "202607170038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workbench_operator_requests"
_CONSTRAINT = "ck_workbench_operator_requests_action"
_OLD_CONSTRAINT = (
    "action IN ('supplier_resolution', 'purchase_purpose', 'accounting_resolution', 'decision', "
    "'execute_vendor_bill', 'product_mapping')"
)
_NEW_CONSTRAINT = (
    "action IN ('supplier_resolution', 'purchase_purpose', 'accounting_resolution', 'decision', "
    "'execute_vendor_bill', 'product_mapping', 'product_line_mapping')"
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _NEW_CONSTRAINT)


def downgrade() -> None:
    product_line_rows = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE action = 'product_line_mapping'")
    )
    if product_line_rows:
        raise RuntimeError(
            f"Refusing to downgrade 202607170039: {product_line_rows} operator request ledger row(s) "
            "have action 'product_line_mapping'. They are audit/idempotency history and are not deleted "
            "automatically; resolve them explicitly before downgrading."
        )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _OLD_CONSTRAINT)
