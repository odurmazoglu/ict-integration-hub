"""workbench review classification evidence

Revision ID: 202607170015
Revises: 202607170014
Create Date: 2026-08-12 10:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170015"
down_revision: str | None = "202607170014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_classification_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("matched_rule_id", sa.String(length=120), nullable=True),
        sa.Column("matched_rule_code", sa.String(length=120), nullable=True),
        sa.Column("matched_rule_version", sa.Integer(), nullable=True),
        sa.Column("matched_rule_name", sa.String(length=200), nullable=True),
        sa.Column("workflow", sa.String(length=64), nullable=True),
        sa.Column("classification_code", sa.String(length=64), nullable=True),
        sa.Column("require_review", sa.Boolean(), nullable=False),
        sa.Column("require_business_context", sa.Boolean(), nullable=False),
        sa.Column("conflicting_rules", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "company_id > 0",
            name="ck_wbrce_company_id_positive",
        ),
        sa.CheckConstraint(
            "review_version > 0",
            name="ck_wbrce_review_version_positive",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_wbrce_schema_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wbrce_review_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "review_id",
            "review_version",
            name="uq_wbrce_company_review_version",
        ),
    )
    op.create_index(
        "ix_wbrce_company_review_version",
        "workbench_review_classification_evidence",
        ["company_id", "review_id", "review_version"],
    )
    op.create_index(
        "ix_wbrce_status",
        "workbench_review_classification_evidence",
        ["status"],
    )
    op.create_index(
        "ix_wbrce_rule_code",
        "workbench_review_classification_evidence",
        ["matched_rule_code"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wbrce_rule_code",
        table_name="workbench_review_classification_evidence",
    )
    op.drop_index(
        "ix_wbrce_status",
        table_name="workbench_review_classification_evidence",
    )
    op.drop_index(
        "ix_wbrce_company_review_version",
        table_name="workbench_review_classification_evidence",
    )
    op.drop_table("workbench_review_classification_evidence")
