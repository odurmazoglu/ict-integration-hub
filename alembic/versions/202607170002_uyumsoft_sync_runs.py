"""uyumsoft sync runs

Revision ID: 202607170002
Revises: 202607170001
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170002"
down_revision: str | None = "202607170001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uyumsoft_sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_directions", sa.JSON(), nullable=False),
        sa.Column("from_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("to_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("current_direction", sa.String(length=16), nullable=True),
        sa.Column("current_page", sa.Integer(), nullable=True),
        sa.Column("pages_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invoices_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor_state", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("failure_message", sa.String(length=1000), nullable=True),
        sa.Column("failure_detail", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_uyumsoft_sync_runs_provider_status",
        "uyumsoft_sync_runs",
        ["provider", "status"],
    )
    op.create_index("ix_uyumsoft_sync_runs_window", "uyumsoft_sync_runs", ["from_date", "to_date"])
    op.create_index("ix_uyumsoft_sync_runs_started_at", "uyumsoft_sync_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_uyumsoft_sync_runs_started_at", table_name="uyumsoft_sync_runs")
    op.drop_index("ix_uyumsoft_sync_runs_window", table_name="uyumsoft_sync_runs")
    op.drop_index("ix_uyumsoft_sync_runs_provider_status", table_name="uyumsoft_sync_runs")
    op.drop_table("uyumsoft_sync_runs")
