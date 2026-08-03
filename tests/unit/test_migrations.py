from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.core.config import get_settings
from app.models.workbench_review_item import REVIEW_AMOUNT_PRECISION, REVIEW_AMOUNT_SCALE, WorkbenchReviewItem


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
    assert "workbench_review_items" in inspector.get_table_names()
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
    review_column_definitions = inspector.get_columns("workbench_review_items")
    review_columns = {column["name"] for column in review_column_definitions}
    assert {
        "review_id",
        "company_id",
        "invoice_id",
        "invoice_number",
        "supplier_tax_number",
        "supplier_name",
        "invoice_date",
        "currency",
        "total_amount",
        "workflow",
        "status",
        "review_reasons",
        "warnings",
        "created_at",
        "updated_at",
        "version",
        "idempotency_key",
    }.issubset(review_columns)
    review_indexes = {index["name"] for index in inspector.get_indexes("workbench_review_items")}
    assert {
        "ix_workbench_review_items_company_id",
        "ix_workbench_review_items_status",
        "ix_workbench_review_items_created_at",
        "ix_workbench_review_items_supplier_tax_number",
        "ix_workbench_review_items_company_status_created",
    }.issubset(review_indexes)
    review_unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("workbench_review_items")
    }
    assert {
        "uq_workbench_review_items_review_id",
        "uq_workbench_review_items_company_idempotency_key",
    }.issubset(review_unique_constraints)
    review_total_amount_type = next(
        column["type"] for column in review_column_definitions if column["name"] == "total_amount"
    )
    model_total_amount_type = WorkbenchReviewItem.__table__.c.total_amount.type
    assert review_total_amount_type.precision == model_total_amount_type.precision == REVIEW_AMOUNT_PRECISION
    assert review_total_amount_type.scale == model_total_amount_type.scale == REVIEW_AMOUNT_SCALE

    command.downgrade(config, "-1")
    inspector = inspect(create_engine(database_url))
    assert "workbench_review_items" not in inspector.get_table_names()
    assert "odoo_draft_invoices" in inspector.get_table_names()
    assert "invoice_documents" in inspector.get_table_names()
    assert "uyumsoft_sync_runs" in inspector.get_table_names()
    assert "uyumsoft_invoice_metadata" in inspector.get_table_names()

    command.upgrade(config, "head")
    inspector = inspect(create_engine(database_url))
    assert "uyumsoft_invoice_metadata" in inspector.get_table_names()
    assert "uyumsoft_sync_runs" in inspector.get_table_names()
    assert "invoice_documents" in inspector.get_table_names()
    assert "odoo_draft_invoices" in inspector.get_table_names()
    assert "workbench_review_items" in inspector.get_table_names()
    get_settings.cache_clear()
