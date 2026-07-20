from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.core.config import get_settings


def test_uyumsoft_invoice_metadata_migration_upgrade_and_downgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    inspector = inspect(create_engine(database_url))
    assert "uyumsoft_invoice_metadata" in inspector.get_table_names()
    assert "uyumsoft_sync_runs" in inspector.get_table_names()
    assert "invoice_documents" in inspector.get_table_names()
    assert "odoo_draft_invoices" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("uyumsoft_invoice_metadata")}
    assert {
        "provider",
        "direction",
        "provider_invoice_id",
        "ettn",
        "identity_key",
        "raw_metadata",
        "first_seen_at",
        "last_seen_at",
    }.issubset(columns)
    document_columns = {column["name"] for column in inspector.get_columns("invoice_documents")}
    assert {
        "invoice_id",
        "provider",
        "direction",
        "document_type",
        "storage_backend",
        "storage_key",
        "content_hash_sha256",
        "content_size_bytes",
    }.issubset(document_columns)
    draft_columns = {column["name"] for column in inspector.get_columns("odoo_draft_invoices")}
    assert {
        "integration_invoice_id",
        "ettn",
        "odoo_model",
        "odoo_move_id",
        "creation_status",
        "safe_error_category",
        "safe_error_message",
        "attempt_count",
    }.issubset(draft_columns)

    command.downgrade(config, "-1")
    inspector = inspect(create_engine(database_url))
    assert "odoo_draft_invoices" not in inspector.get_table_names()
    assert "invoice_documents" in inspector.get_table_names()
    assert "uyumsoft_sync_runs" in inspector.get_table_names()
    assert "uyumsoft_invoice_metadata" in inspector.get_table_names()

    command.upgrade(config, "head")
    inspector = inspect(create_engine(database_url))
    assert "uyumsoft_invoice_metadata" in inspector.get_table_names()
    assert "uyumsoft_sync_runs" in inspector.get_table_names()
    assert "invoice_documents" in inspector.get_table_names()
    assert "odoo_draft_invoices" in inspector.get_table_names()
    get_settings.cache_clear()
