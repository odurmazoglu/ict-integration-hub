"""workbench decision quotation scenario selection

Revision ID: 202607170018
Revises: 202607170017
Create Date: 2026-09-03 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170018"
down_revision: str | None = "202607170017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workbench_review_decisions") as batch_op:
        batch_op.add_column(sa.Column("selected_quotation_scenario_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workbench_review_decisions") as batch_op:
        batch_op.drop_column("selected_quotation_scenario_ids")
