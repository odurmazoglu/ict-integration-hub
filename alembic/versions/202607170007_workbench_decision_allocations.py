"""workbench decision allocations

Revision ID: 202607170007
Revises: 202607170006
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170007"
down_revision: str | None = "202607170006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workbench_review_decisions",
        sa.Column("business_context_allocations", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workbench_review_decisions", "business_context_allocations")
