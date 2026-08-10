"""execution source evidence capture constraints

Revision ID: 202607170012
Revises: 202607170011
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170012"
down_revision: str | None = "202607170011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("execution_source_invoice_evidence") as batch_op:
        batch_op.add_column(sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False))
        batch_op.create_check_constraint(
            "ck_execution_source_invoice_evidence_schema_version_positive",
            "schema_version > 0",
        )
        batch_op.create_unique_constraint(
            "uq_execution_source_invoice_evidence_decision_id",
            ["decision_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_source_invoice_evidence") as batch_op:
        batch_op.drop_constraint("uq_execution_source_invoice_evidence_decision_id", type_="unique")
        batch_op.drop_constraint(
            "ck_execution_source_invoice_evidence_schema_version_positive",
            type_="check",
        )
        batch_op.drop_column("schema_version")
