"""add sale_ok to workbench product line creations (V1 product creation)

Revision ID: 202607170044
Revises: 202607170043
Create Date: 2026-10-23 16:00:00.000000

Additive only: one nullable boolean ``sale_ok`` column on
``workbench_product_line_creations`` -- the operator's explicit "Satılabilir" choice for a
new Workbench product. It is part of the recorded intent (and of its digest, to which the
consumed write authorization is bound), so crash recovery and replay re-create or adopt
exactly the requested product. Every existing row keeps ``NULL`` = created before the
choice existed; its recorded digest is unchanged. No other table, column, index or
constraint changes.

The downgrade drops the column and refuses while any row holds a value: without it those
rows' intents no longer match their digests and could not be recovered or replayed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170044"
down_revision: str | None = "202607170043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workbench_product_line_creations"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("sale_ok", sa.Boolean(), nullable=True))


def downgrade() -> None:
    recorded = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE sale_ok IS NOT NULL")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"Refusing to drop {_TABLE}.sale_ok: {recorded} product line creation(s) record an explicit "
            "sale_ok choice that their intent digest depends on."
        )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("sale_ok")
