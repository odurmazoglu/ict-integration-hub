from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.core.config import get_settings
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import REVIEW_AMOUNT_PRECISION, REVIEW_AMOUNT_SCALE, WorkbenchReviewItem
from app.models.workflow_execution import WorkflowExecution


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
    assert "workbench_review_decisions" in inspector.get_table_names()
    assert "workflow_executions" in inspector.get_table_names()
    assert "workflow_execution_steps" in inspector.get_table_names()
    assert "workflow_execution_events" in inspector.get_table_names()
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
    decision_columns = {column["name"] for column in inspector.get_columns("workbench_review_decisions")}
    assert {
        "decision_id",
        "review_id",
        "company_id",
        "review_version_before",
        "review_version_after",
        "decision_type",
        "selected_workflow",
        "selected_partner_id",
        "line_resolutions",
        "tax_resolutions",
        "business_context",
        "business_context_allocations",
        "comment",
        "decided_by",
        "idempotency_key",
        "submitted_at",
    }.issubset(decision_columns)
    decision_indexes = {index["name"] for index in inspector.get_indexes("workbench_review_decisions")}
    assert {
        "ix_workbench_review_decisions_company_id",
        "ix_workbench_review_decisions_review_id",
        "ix_workbench_review_decisions_decision_type",
        "ix_workbench_review_decisions_submitted_at",
        "ix_workbench_review_decisions_company_review",
    }.issubset(decision_indexes)
    decision_unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("workbench_review_decisions")
    }
    assert {
        "uq_workbench_review_decisions_decision_id",
        "uq_workbench_review_decisions_company_idempotency_key",
    }.issubset(decision_unique_constraints)
    assert WorkbenchReviewDecision.__table__.c.review_version_before.nullable is False
    assert WorkbenchReviewDecision.__table__.c.business_context_allocations.nullable is True
    execution_columns = {column["name"] for column in inspector.get_columns("workflow_executions")}
    assert {
        "execution_id",
        "review_id",
        "decision_version",
        "company_id",
        "state",
        "mode",
        "idempotency_key",
        "plan_signature",
        "plan",
        "checkpoint",
        "retry_policy",
        "failure",
        "current_step_key",
        "runtime_version",
        "next_event_sequence",
        "created_at",
        "updated_at",
    }.issubset(execution_columns)
    execution_indexes = {index["name"] for index in inspector.get_indexes("workflow_executions")}
    assert {
        "ix_workflow_executions_review_id",
        "ix_workflow_executions_company_id",
        "ix_workflow_executions_state",
        "ix_workflow_executions_company_review",
    }.issubset(execution_indexes)
    execution_unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("workflow_executions")
    }
    assert {
        "uq_workflow_executions_execution_id",
        "uq_workflow_executions_company_idempotency_key",
    }.issubset(execution_unique_constraints)
    assert WorkflowExecution.__table__.c.checkpoint.nullable is False
    step_columns = {column["name"] for column in inspector.get_columns("workflow_execution_steps")}
    assert {
        "execution_id",
        "step_key",
        "step_type",
        "sequence",
        "state",
        "allocation_keys",
        "retry_count",
        "last_result",
    }.issubset(step_columns)
    event_columns = {column["name"] for column in inspector.get_columns("workflow_execution_events")}
    assert {
        "event_id",
        "execution_id",
        "sequence",
        "event_type",
        "state",
        "step_key",
        "step_type",
        "data",
        "occurred_at",
    }.issubset(event_columns)

    command.downgrade(config, "-1")
    inspector = inspect(create_engine(database_url))
    assert "workflow_executions" not in inspector.get_table_names()
    assert "workflow_execution_steps" not in inspector.get_table_names()
    assert "workflow_execution_events" not in inspector.get_table_names()
    assert "workbench_review_decisions" in inspector.get_table_names()
    decision_columns_after_runtime_downgrade = {
        column["name"] for column in inspector.get_columns("workbench_review_decisions")
    }
    assert "business_context_allocations" in decision_columns_after_runtime_downgrade

    command.downgrade(config, "-1")
    inspector = inspect(create_engine(database_url))
    assert "workbench_review_decisions" in inspector.get_table_names()
    decision_columns_after_allocation_downgrade = {
        column["name"] for column in inspector.get_columns("workbench_review_decisions")
    }
    assert "business_context_allocations" not in decision_columns_after_allocation_downgrade

    command.downgrade(config, "-1")
    inspector = inspect(create_engine(database_url))
    assert "workbench_review_decisions" not in inspector.get_table_names()
    assert "workbench_review_items" in inspector.get_table_names()

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
    assert "workbench_review_decisions" in inspector.get_table_names()
    assert "workflow_executions" in inspector.get_table_names()
    assert "workflow_execution_steps" in inspector.get_table_names()
    assert "workflow_execution_events" in inspector.get_table_names()
    get_settings.cache_clear()
