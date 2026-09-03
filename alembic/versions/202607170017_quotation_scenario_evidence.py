"""quotation scenario evidence

Revision ID: 202607170017
Revises: 202607170016
Create Date: 2026-09-03 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170017"
down_revision: str | None = "202607170016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quotation_scenario_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("decision_id", sa.String(length=255), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("scenario_id", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("scenario_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("company_id > 0", name="ck_quotation_scenario_evidence_company_positive"),
        sa.CheckConstraint(
            "decision_version > 0",
            name="ck_quotation_scenario_evidence_decision_version_positive",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_quotation_scenario_evidence_schema_version_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "review_id",
            "decision_id",
            "decision_version",
            "scenario_id",
            name="uq_quotation_scenario_evidence_semantic_identity",
        ),
    )
    op.create_index(
        "ix_quotation_scenario_evidence_company_review_decision",
        "quotation_scenario_evidence",
        ["company_id", "review_id", "decision_id", "decision_version"],
    )
    op.create_index(
        "ix_quotation_scenario_evidence_scenario_id",
        "quotation_scenario_evidence",
        ["scenario_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quotation_scenario_evidence_scenario_id",
        table_name="quotation_scenario_evidence",
    )
    op.drop_index(
        "ix_quotation_scenario_evidence_company_review_decision",
        table_name="quotation_scenario_evidence",
    )
    op.drop_table("quotation_scenario_evidence")
