"""invoice documents

Revision ID: 202607170003
Revises: 202607170002
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170003"
down_revision: str | None = "202607170002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoice_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("uyumsoft_invoice_metadata.id"), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("document_type", sa.String(length=32), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("content_hash_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("content_size_bytes", sa.Integer(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("invoice_id", "document_type", name="uq_invoice_document_invoice_type"),
        sa.UniqueConstraint("storage_backend", "storage_key", name="uq_invoice_document_storage_key"),
    )
    op.create_index(
        "ix_invoice_documents_provider_direction",
        "invoice_documents",
        ["provider", "direction"],
    )
    op.create_index("ix_invoice_documents_content_hash", "invoice_documents", ["content_hash_sha256"])


def downgrade() -> None:
    op.drop_index("ix_invoice_documents_content_hash", table_name="invoice_documents")
    op.drop_index("ix_invoice_documents_provider_direction", table_name="invoice_documents")
    op.drop_table("invoice_documents")
