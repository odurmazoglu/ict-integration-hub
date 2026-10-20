"""add internal_use_accounting_pin to execution source invoice evidence (P0 INTERNAL_USE)

Revision ID: 202607170043
Revises: 202607170042
Create Date: 2026-10-20 16:00:00.000000

Additive only: one nullable JSON ``internal_use_accounting_pin`` column on
``execution_source_invoice_evidence`` -- the immutable, decision-scoped INTERNAL_USE
expense-account pin written atomically with an accepted Vendor Bill decision for a
product-backed INTERNAL_USE review. Every existing row keeps ``NULL`` (no historical
decision is reinterpreted). No other table, column, index, or constraint changes. The
downgrade drops the column; run it only while no row holds a pin (otherwise those
decisions' accepted accounting evidence is lost and they can no longer execute).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170043"
down_revision: str | None = "202607170042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "execution_source_invoice_evidence"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("internal_use_accounting_pin", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("internal_use_accounting_pin")
