from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import app.application
from app.application.commands import ImportInvoiceCommand, ImportSessionCommand
from app.application.dto import ImportInvoiceResult, ImportSessionResult
from app.application.use_cases import ImportSession
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party


@pytest.mark.asyncio
async def test_empty_session_completes_without_invoice_calls() -> None:
    importer = FakeImportInvoiceUseCase()
    session = ImportSession(import_invoice_use_case=importer)

    result = await session.execute(ImportSessionCommand(session_id="session-1"))

    assert result.session_id == "session-1"
    assert result.status == "COMPLETED"
    assert session.status == "COMPLETED"
    assert result.started_at.tzinfo is UTC
    assert result.finished_at.tzinfo is UTC
    assert result.finished_at >= result.started_at
    assert result.duration >= 0
    assert result.processed == 0
    assert result.successful == 0
    assert result.duplicates == 0
    assert result.failed == 0
    assert result.results == ()
    assert importer.commands == []


@pytest.mark.asyncio
async def test_single_invoice_session_delegates_to_import_invoice_use_case() -> None:
    invoice = _invoice("INV-1", "ETTN-1")
    importer = FakeImportInvoiceUseCase(
        results=[ImportInvoiceResult(success=True, invoice_id="ETTN-1", status="dry_run", warnings=("dry",))]
    )
    session = ImportSession(import_invoice_use_case=importer)

    result = await session.execute(
        ImportSessionCommand(
            invoices=(invoice,),
            session_id="session-1",
            company_id=7,
            dry_run=True,
            approved_by="finance-user",
        )
    )

    assert result.status == "COMPLETED"
    assert result.processed == 1
    assert result.successful == 1
    assert result.failed == 0
    assert result.warnings == ("dry",)
    assert importer.commands == [
        ImportInvoiceCommand(
            invoice=invoice,
            idempotency_key="ETTN-1",
            company_id=7,
            dry_run=True,
            approved_by="finance-user",
        )
    ]


@pytest.mark.asyncio
async def test_multiple_invoice_session_executes_sequentially() -> None:
    invoice_1 = _invoice("INV-1", "ETTN-1")
    invoice_2 = _invoice("INV-2", "ETTN-2")
    invoice_3 = _invoice("INV-3", "ETTN-3")
    importer = FakeImportInvoiceUseCase(
        results=[
            ImportInvoiceResult(success=True, invoice_id="ETTN-1", status="dry_run"),
            ImportInvoiceResult(success=True, invoice_id="ETTN-2", status="created", vendor_bill_id=42),
            ImportInvoiceResult(success=True, invoice_id="ETTN-3", status="dry_run"),
        ]
    )

    result = await ImportSession(import_invoice_use_case=importer).execute(
        ImportSessionCommand(invoices=(invoice_1, invoice_2, invoice_3), session_id="session-1")
    )

    assert result.status == "COMPLETED"
    assert result.processed == 3
    assert result.successful == 3
    assert result.failed == 0
    assert [command.invoice.header.invoice_number for command in importer.commands] == ["INV-1", "INV-2", "INV-3"]
    assert [command.idempotency_key for command in importer.commands] == ["ETTN-1", "ETTN-2", "ETTN-3"]


@pytest.mark.asyncio
async def test_duplicate_invoice_is_counted_from_import_invoice_result() -> None:
    importer = FakeImportInvoiceUseCase(
        results=[
            ImportInvoiceResult(
                success=True,
                invoice_id="ETTN-1",
                status="already_imported",
                vendor_bill_id=42,
                warnings=("Invoice was already imported.",),
            )
        ]
    )

    result = await ImportSession(import_invoice_use_case=importer).execute(
        ImportSessionCommand(invoices=(_invoice("INV-1", "ETTN-1"),), session_id="session-1")
    )

    assert result.status == "COMPLETED"
    assert result.successful == 1
    assert result.duplicates == 1
    assert result.failed == 0
    assert result.warnings == ("Invoice was already imported.",)


@pytest.mark.asyncio
async def test_review_required_invoice_is_counted_without_failing_session() -> None:
    importer = FakeImportInvoiceUseCase(
        results=[
            ImportInvoiceResult(
                success=False,
                invoice_id="ETTN-1",
                status="review_required",
                review_required=True,
                warnings=("Manual review required.",),
            )
        ]
    )

    result = await ImportSession(import_invoice_use_case=importer).execute(
        ImportSessionCommand(invoices=(_invoice("INV-1", "ETTN-1"),), session_id="session-1")
    )

    assert result.status == "COMPLETED"
    assert result.processed == 1
    assert result.successful == 0
    assert result.duplicates == 0
    assert result.review_required == 1
    assert result.failed == 0
    assert result.warnings == ("Manual review required.",)


@pytest.mark.asyncio
async def test_failed_invoice_is_collected_without_stopping_session() -> None:
    invoice_1 = _invoice("INV-1", "ETTN-1")
    invoice_2 = _invoice("INV-2", "ETTN-2")
    importer = FakeImportInvoiceUseCase(
        results=[
            SafeApplicationFailure("Partner matching failed safely."),
            ImportInvoiceResult(success=True, invoice_id="ETTN-2", status="dry_run"),
        ]
    )

    result = await ImportSession(import_invoice_use_case=importer).execute(
        ImportSessionCommand(invoices=(invoice_1, invoice_2), session_id="session-1")
    )

    assert result.status == "FAILED"
    assert result.processed == 2
    assert result.successful == 1
    assert result.failed == 1
    assert result.errors == ("Partner matching failed safely.",)
    assert result.results[0] == ImportInvoiceResult(
        success=False,
        invoice_id="ETTN-1",
        status="failed",
        errors=("Partner matching failed safely.",),
        duration=result.results[0].duration,
    )
    assert result.results[1].success is True
    assert [command.idempotency_key for command in importer.commands] == ["ETTN-1", "ETTN-2"]


@pytest.mark.asyncio
async def test_mixed_success_duplicate_and_failure_summary() -> None:
    importer = FakeImportInvoiceUseCase(
        results=[
            ImportInvoiceResult(success=True, invoice_id="ETTN-1", status="created", vendor_bill_id=42),
            ImportInvoiceResult(success=True, invoice_id="ETTN-2", status="already_exists", vendor_bill_id=43),
            RuntimeError("raw transport details should not leak"),
        ]
    )

    result = await ImportSession(import_invoice_use_case=importer).execute(
        ImportSessionCommand(
            invoices=(
                _invoice("INV-1", "ETTN-1"),
                _invoice("INV-2", "ETTN-2"),
                _invoice("INV-3", "ETTN-3"),
            ),
            session_id="session-1",
        )
    )

    assert result.status == "FAILED"
    assert result.processed == 3
    assert result.successful == 2
    assert result.duplicates == 1
    assert result.review_required == 0
    assert result.failed == 1
    assert result.errors == ("Invoice import failed.",)
    assert [item.status for item in result.results] == ["created", "already_exists", "failed"]


def test_import_session_dtos_are_immutable() -> None:
    command = ImportSessionCommand(invoices=(_invoice("INV-1", "ETTN-1"),))
    result = ImportSessionResult(
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
        result.status = "FAILED"


def test_import_session_is_exported_from_application_package() -> None:
    assert app.application.ImportSession is ImportSession


def test_import_session_does_not_import_disallowed_boundaries() -> None:
    content = Path("app/application/use_cases/import_session.py").read_text()
    forbidden_terms = (
        "app.connectors",
        "app.models",
        "app.db",
        "app.erp",
        "app.matching",
        "app.billing",
        "app.tax_mapping",
        "fastapi",
        "sqlalchemy",
        "httpx",
        "zeep",
        "rule_engine",
        "decision_engine",
        "ai_advisor",
        "Odoo",
        "VendorBill",
        "create_account_move",
        "search_read",
    )

    for forbidden in forbidden_terms:
        assert forbidden not in content, f"ImportSession depends on {forbidden}"


class FakeImportInvoiceUseCase:
    def __init__(self, *, results: list[ImportInvoiceResult | Exception] | None = None) -> None:
        self.results = list(results or [])
        self.commands: list[ImportInvoiceCommand] = []

    async def execute(self, command: ImportInvoiceCommand) -> ImportInvoiceResult:
        self.commands.append(command)
        if not self.results:
            return ImportInvoiceResult(success=True, invoice_id=command.idempotency_key, status="dry_run")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SafeApplicationFailure(Exception):
    def __init__(self, safe_message: str) -> None:
        super().__init__("unsafe provider details")
        self.safe_message = safe_message


def _invoice(invoice_number: str, ettn: str | None) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number=invoice_number,
            invoice_uuid=f"{invoice_number}-UUID",
            ettn=ettn,
            issue_date=date(2026, 7, 30),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Line 1",
                buyer_item_code="SKU-1",
                quantity=Decimal("2"),
                unit_code="NIU",
                unit_price=Decimal("50"),
            ),
        ),
    )
