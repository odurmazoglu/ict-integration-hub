"""execution customer billing evidence

Revision ID: 202607170014
Revises: 202607170013
Create Date: 2026-08-11 11:25:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170014"
down_revision: str | None = "202607170013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_customer_billing_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.String(length=255), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("billing_key", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("billing_instruction", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("company_id > 0", name="ck_exec_cust_bill_ev_company_positive"),
        sa.CheckConstraint(
            "decision_version > 0",
            name="ck_exec_cust_bill_ev_decision_version_positive",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_exec_cust_bill_ev_schema_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["workbench_review_decisions.decision_id"],
            name="fk_execution_customer_billing_evidence_decision_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_id",
            "billing_key",
            name="uq_execution_customer_billing_evidence_decision_key",
        ),
    )
    op.create_index(
        "ix_execution_customer_billing_evidence_company_review_version",
        "execution_customer_billing_evidence",
        ["company_id", "review_id", "decision_version"],
    )
    op.create_index(
        "ix_execution_customer_billing_evidence_decision_id",
        "execution_customer_billing_evidence",
        ["decision_id"],
    )
    op.create_index(
        "ix_execution_customer_billing_evidence_billing_key",
        "execution_customer_billing_evidence",
        ["billing_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_customer_billing_evidence_billing_key",
        table_name="execution_customer_billing_evidence",
    )
    op.drop_index(
        "ix_execution_customer_billing_evidence_decision_id",
        table_name="execution_customer_billing_evidence",
    )
    op.drop_index(
        "ix_execution_customer_billing_evidence_company_review_version",
        table_name="execution_customer_billing_evidence",
    )
    op.drop_table("execution_customer_billing_evidence")
