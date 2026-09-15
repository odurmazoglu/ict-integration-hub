"""account-only expense execution evidence

Revision ID: 202607170025
Revises: 202607170024
Create Date: 2026-09-15 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170025"
down_revision: str | None = "202607170024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workbench_review_execution_evidence") as batch_op:
        batch_op.add_column(sa.Column("account_only_expense_match", sa.JSON(), nullable=True))
    with op.batch_alter_table("execution_source_invoice_evidence") as batch_op:
        batch_op.add_column(sa.Column("account_only_expense_match", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("execution_source_invoice_evidence") as batch_op:
        batch_op.drop_column("account_only_expense_match")
    with op.batch_alter_table("workbench_review_execution_evidence") as batch_op:
        batch_op.drop_column("account_only_expense_match")
