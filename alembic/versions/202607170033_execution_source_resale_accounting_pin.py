"""add resale_accounting_pin to execution source invoice evidence (P0-PROD-18F-1)

Revision ID: 202607170033
Revises: 202607170032
Create Date: 2026-09-25 12:00:00.000000

Additive only: one nullable JSON ``resale_accounting_pin`` column on
``execution_source_invoice_evidence`` -- the immutable, decision-scoped evidence row
written atomically with each accepted Vendor Bill decision. Every existing row keeps
``NULL`` (no historical decision is reinterpreted). No other table, column, index, or
constraint changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170033"
down_revision: str | None = "202607170032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "execution_source_invoice_evidence"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("resale_accounting_pin", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("resale_accounting_pin")
