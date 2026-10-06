"""Workbench operator request ledger (ADR-0013)

Revision ID: 202607170036
Revises: 202607170035
Create Date: 2026-10-06 12:00:00.000000

One new append-mostly table, ``workbench_operator_requests``: the Hub's audit and
idempotency ledger for typed operator requests read from the Odoo Workbench. It holds
no business state (resolutions, decisions and executions stay in their existing
tables) and has no foreign key to ``workbench_review_items`` on purpose: it records
untrusted Odoo input, including requests naming an unknown review, which are then
rejected.

Downgrade drops the table. That loses only request audit/idempotency history, never
business state; a request still pending in Odoo after a downgrade would be treated as
new by a later re-upgrade, and the existing use cases' own replay rules keep that safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170036"
down_revision: str | None = "202607170035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workbench_operator_requests"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("odoo_record_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("requested_by_odoo_user_id", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("authorization_id", sa.String(length=36), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("company_id > 0", name="ck_workbench_operator_requests_company_pos"),
        sa.CheckConstraint("odoo_record_id > 0", name="ck_workbench_operator_requests_odoo_record_pos"),
        sa.CheckConstraint("expected_version > 0", name="ck_workbench_operator_requests_version_pos"),
        sa.CheckConstraint("requested_by_odoo_user_id > 0", name="ck_workbench_operator_requests_requester_pos"),
        sa.CheckConstraint("attempts >= 0", name="ck_workbench_operator_requests_attempts"),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'stale', 'rejected', 'unauthorized', 'failed')",
            name="ck_workbench_operator_requests_status",
        ),
        sa.CheckConstraint(
            "action IN ('supplier_resolution', 'purchase_purpose', 'accounting_resolution', 'decision', "
            "'execute_vendor_bill')",
            name="ck_workbench_operator_requests_action",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_key", name="uq_workbench_operator_requests_request_key"),
    )
    op.create_index("ix_workbench_operator_requests_review", _TABLE, ["company_id", "review_id"])


def downgrade() -> None:
    op.drop_index("ix_workbench_operator_requests_review", table_name=_TABLE)
    op.drop_table(_TABLE)
