from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import app.application
from app.application.commands import Command, ImportSessionCommand, VendorBillWriteCommand
from app.application.dto import ApplicationDTO, ImportSessionResult, VendorBillWriteResult
from app.application.exceptions import ApplicationError
from app.application.ports import InvoiceImportHistory, VendorBillWriter
from app.application.queries import Query
from app.application.services import UnitOfWork
from app.application.use_cases import ImportInvoiceUseCase, ImportSession, UseCase
from app.billing import VendorBill, VendorBillLine


def test_application_foundation_exports_core_conventions() -> None:
    assert app.application.ApplicationDTO is ApplicationDTO
    assert app.application.Command is Command
    assert app.application.Query is Query
    assert app.application.UseCase is UseCase
    assert app.application.ApplicationError is ApplicationError
    assert app.application.ImportInvoiceUseCase is ImportInvoiceUseCase
    assert app.application.ImportSession is ImportSession


def test_application_dtos_are_immutable() -> None:
    command = VendorBillWriteCommand(vendor_bill=_vendor_bill(), idempotency_key="ettn:abc")
    session_command = ImportSessionCommand()
    result = VendorBillWriteResult(status="dry_run", idempotency_key="ettn:abc")
    session_result = ImportSessionResult(
        session_id="session-1",
        status="COMPLETED",
        started_at=datetime(2026, 7, 30, tzinfo=UTC),
        finished_at=datetime(2026, 7, 30, tzinfo=UTC),
        duration=0,
        processed=0,
        successful=0,
        duplicates=0,
        failed=0,
    )

    with pytest.raises(FrozenInstanceError):
        command.dry_run = False
    with pytest.raises(FrozenInstanceError):
        session_command.dry_run = False
    with pytest.raises(FrozenInstanceError):
        result.status = "created"
    with pytest.raises(FrozenInstanceError):
        session_result.status = "FAILED"


def test_vendor_bill_writer_port_is_protocol_only() -> None:
    assert hasattr(VendorBillWriter, "write_vendor_bill")
    assert hasattr(InvoiceImportHistory, "find_imported_invoice")
    assert hasattr(UnitOfWork, "commit")
    assert hasattr(UnitOfWork, "rollback")


def test_application_layer_does_not_import_infrastructure_boundaries() -> None:
    application_root = Path("app/application")
    forbidden_imports = (
        "app.connectors",
        "app.models",
        "app.db",
        "fastapi",
        "sqlalchemy",
        "httpx",
        "zeep",
    )

    for path in application_root.rglob("*.py"):
        content = path.read_text()
        for forbidden in forbidden_imports:
            assert forbidden not in content, f"{path} imports {forbidden}"


def test_import_invoice_use_case_does_not_import_future_workflow_engines_or_providers() -> None:
    content = Path("app/application/use_cases/import_invoice.py").read_text()
    forbidden_terms = (
        "app.connectors",
        "app.models",
        "app.db",
        "fastapi",
        "sqlalchemy",
        "httpx",
        "zeep",
        "rule_engine",
        "decision_engine",
        "ai_advisor",
        "import_session",
        "OdooJson2Client",
        "OdooDraftInvoiceService",
    )

    for forbidden in forbidden_terms:
        assert forbidden not in content, f"ImportInvoiceUseCase depends on {forbidden}"


def _vendor_bill() -> VendorBill:
    return VendorBill(
        supplier_id=1,
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 30),
        currency="TRY",
        external_uuid="ettn-1",
        reference="INV-1",
        invoice_lines=(
            VendorBillLine(
                product_id=10,
                quantity=Decimal("1"),
                uom=None,
                unit_price=Decimal("100"),
                tax_ids=(20,),
                description="Line",
            ),
        ),
    )
