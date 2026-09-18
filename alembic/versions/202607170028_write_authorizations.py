"""write authorizations table (P0-PROD-09D1)

Revision ID: 202607170028
Revises: 202607170027
Create Date: 2026-09-18 15:00:00.000000

DB-backed, short-lived, single-use write authorization for operator-driven
production execution without container restarts.

Constraint/index names use the abbreviated 'wr_write_auth' prefix to stay well
within PostgreSQL's 63-byte NAMEDATALEN limit.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170028"
down_revision: str | None = "202607170027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_review_write_authorizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("authorization_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("authorized_by", sa.String(length=255), nullable=False),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_trace_id", sa.String(length=255), nullable=True),
        sa.Column("consumed_by_execution_id", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=255), nullable=True),
        sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_trace_id", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wr_write_auth_review_id",
        ),
        sa.UniqueConstraint("authorization_id", name="uq_wr_write_auth_id"),
        sa.CheckConstraint("company_id > 0", name="ck_wr_write_auth_company_pos"),
        sa.CheckConstraint("target_version > 0", name="ck_wr_write_auth_version_pos"),
        sa.CheckConstraint("use_count >= 0", name="ck_wr_write_auth_use_count"),
        sa.CheckConstraint(
            "status != 'consumed' OR (consumed_at IS NOT NULL "
            "AND consumed_by_execution_id IS NOT NULL AND use_count > 0)",
            name="ck_wr_write_auth_consumed",
        ),
        sa.CheckConstraint(
            "operation_type IN ('EXECUTE_VENDOR_BILL')",
            name="ck_wr_write_auth_op_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'revoked')",
            name="ck_wr_write_auth_status",
        ),
    )
    op.create_index(
        "ix_wr_write_auth_lookup",
        "workbench_review_write_authorizations",
        ["company_id", "review_id", "operation_type", "target_version"],
    )
    op.create_index(
        "ix_wr_write_auth_status",
        "workbench_review_write_authorizations",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wr_write_auth_status",
        table_name="workbench_review_write_authorizations",
    )
    op.drop_index(
        "ix_wr_write_auth_lookup",
        table_name="workbench_review_write_authorizations",
    )
    op.drop_table("workbench_review_write_authorizations")
