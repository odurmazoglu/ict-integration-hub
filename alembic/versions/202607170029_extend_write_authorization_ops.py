"""extend write authorization operation types (P0-PROD-09F)

Revision ID: 202607170029
Revises: 202607170028
Create Date: 2026-09-19 09:00:00.000000

Extends ck_wr_write_auth_op_type to also permit CREATE_PERMANENT_SUPPLIER,
ONE_OFF_VENDOR_SUPPLIER, and ONE_OFF_VENDOR_ARCHIVE -- narrow runtime
authorization for supplier remediation and ONE_OFF_VENDOR archive/recovery,
reusing the exact P0-PROD-09D1 write-authorization table/state machine
unchanged. No column, index, or other constraint changes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202607170029"
down_revision: str | None = "202607170028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CONSTRAINT = "operation_type IN ('EXECUTE_VENDOR_BILL')"
_NEW_CONSTRAINT = (
    "operation_type IN ('EXECUTE_VENDOR_BILL', 'CREATE_PERMANENT_SUPPLIER', "
    "'ONE_OFF_VENDOR_SUPPLIER', 'ONE_OFF_VENDOR_ARCHIVE')"
)


def upgrade() -> None:
    with op.batch_alter_table("workbench_review_write_authorizations") as batch_op:
        batch_op.drop_constraint("ck_wr_write_auth_op_type", type_="check")
        batch_op.create_check_constraint("ck_wr_write_auth_op_type", _NEW_CONSTRAINT)


def downgrade() -> None:
    with op.batch_alter_table("workbench_review_write_authorizations") as batch_op:
        batch_op.drop_constraint("ck_wr_write_auth_op_type", type_="check")
        batch_op.create_check_constraint("ck_wr_write_auth_op_type", _OLD_CONSTRAINT)
