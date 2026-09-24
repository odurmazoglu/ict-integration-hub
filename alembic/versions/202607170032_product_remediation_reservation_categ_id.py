"""add categ_id to product remediation reservations (P0-PROD-18E-2)

Revision ID: 202607170032
Revises: 202607170031
Create Date: 2026-09-24 12:00:00.000000

Additive only: one nullable ``categ_id`` column (plus a NULL-tolerant positivity
check) on ``workbench_review_product_remediation_reservations``. Existing rows keep
``NULL`` and stay valid; the application requires ``categ_id`` for a RESALE
CREATE_NEW_PRODUCT reservation. No other table, column, index, or constraint changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170032"
down_revision: str | None = "202607170031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workbench_review_product_remediation_reservations"
_CHECK = "ck_wrpr_reservations_categ_id_positive"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("categ_id", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(_CHECK, "categ_id IS NULL OR categ_id > 0")


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_column("categ_id")
