"""allow the product_mapping action in the operator request ledger

Revision ID: 202607170038
Revises: 202607170037
Create Date: 2026-10-07 22:00:00.000000

#208 added the ``product_mapping`` Workbench operator action ("Ürün Eşleştir") but
202607170037 only widened the write-authorization operation types; the ledger's
ck_workbench_operator_requests_action still rejected the action, so the poller could
not record a product-mapping request. This widens exactly that CHECK constraint.
No column, index, other constraint or data change.

Downgrade refuses (no row is deleted or changed) while any ledger row has
action = 'product_mapping'; restoring the narrower constraint would otherwise fail
validation or, worse, need those audit rows removed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170038"
down_revision: str | None = "202607170037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workbench_operator_requests"
_CONSTRAINT = "ck_workbench_operator_requests_action"
_OLD_CONSTRAINT = (
    "action IN ('supplier_resolution', 'purchase_purpose', 'accounting_resolution', 'decision', 'execute_vendor_bill')"
)
_NEW_CONSTRAINT = (
    "action IN ('supplier_resolution', 'purchase_purpose', 'accounting_resolution', 'decision', "
    "'execute_vendor_bill', 'product_mapping')"
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _NEW_CONSTRAINT)


def downgrade() -> None:
    product_mapping_rows = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE action = 'product_mapping'")
    )
    if product_mapping_rows:
        raise RuntimeError(
            f"Refusing to downgrade 202607170038: {product_mapping_rows} operator request ledger row(s) "
            "have action 'product_mapping'. They are audit/idempotency history and are not deleted "
            "automatically; resolve them explicitly before downgrading."
        )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _OLD_CONSTRAINT)
