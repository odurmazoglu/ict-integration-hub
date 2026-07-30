from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

import app.application
from app.application.commands import ImportInvoiceCommand
from app.application.dto import DecisionResult, ExistingInvoiceImport, ImportInvoiceResult
from app.application.use_cases import (
    ImportInvoiceInfrastructureError,
    ImportInvoiceUseCase,
    ImportInvoiceValidationError,
)
from app.application.workflow import WorkflowType
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party


@pytest.mark.asyncio
async def test_import_invoice_delegates_to_decision_engine_after_duplicate_check() -> None:
    decision_engine = FakeDecisionEngine(
        DecisionResult(
            success=True,
            invoice_id="INV-ETTN",
            workflow=WorkflowType.VENDOR_BILL,
            strategy=WorkflowType.VENDOR_BILL.value,
            status="dry_run",
            warnings=("Decision completed.",),
        )
    )
    use_case = _use_case(decision_engine=decision_engine)
    command = ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)

    result = await use_case.execute(command)

    assert result.success is True
    assert result.invoice_id == "INV-ETTN"
    assert result.status == "dry_run"
    assert result.vendor_bill_id is None
    assert result.warnings == ("Decision completed.",)
    assert result.errors == ()
    assert result.duration >= 0
    assert use_case.import_history.calls == ["ettn:INV-ETTN"]
    assert decision_engine.commands == [command]


@pytest.mark.asyncio
async def test_duplicate_import_short_circuits_decision_engine() -> None:
    decision_engine = FakeDecisionEngine()
    use_case = _use_case(
        existing=ExistingInvoiceImport(invoice_id="INV-ETTN", vendor_bill_id=42),
        decision_engine=decision_engine,
    )

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result == ImportInvoiceResult(
        success=True,
        invoice_id="INV-ETTN",
        status="already_imported",
        vendor_bill_id=42,
        warnings=("Invoice was already imported.",),
        duration=result.duration,
    )
    assert decision_engine.commands == []


@pytest.mark.asyncio
async def test_decision_existing_result_is_returned_without_erp_model_leakage() -> None:
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(
            DecisionResult(
                success=True,
                invoice_id="INV-ETTN",
                workflow=WorkflowType.VENDOR_BILL,
                strategy=WorkflowType.VENDOR_BILL.value,
                status="already_exists",
                vendor_bill_id=99,
                warnings=("Existing draft found.",),
            )
        )
    )

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result.success is True
    assert result.status == "already_exists"
    assert result.vendor_bill_id == 99
    assert result.warnings == ("Existing draft found.",)
    assert not hasattr(result, "external_model")


@pytest.mark.asyncio
async def test_decision_failed_result_is_returned_as_safe_failure_result() -> None:
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(
            DecisionResult(
                success=False,
                invoice_id="INV-ETTN",
                workflow=WorkflowType.VENDOR_BILL,
                strategy=WorkflowType.VENDOR_BILL.value,
                status="failed",
                errors=("Vendor Bill write failed safely.",),
            )
        )
    )

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result.success is False
    assert result.status == "failed"
    assert result.errors == ("Vendor Bill write failed safely.",)


@pytest.mark.asyncio
async def test_missing_idempotency_key_is_application_validation_error() -> None:
    use_case = _use_case()

    with pytest.raises(ImportInvoiceValidationError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key=" "))

    assert exc_info.value.safe_message == "Import idempotency key is required."
    assert use_case.import_history.calls == []


@pytest.mark.asyncio
async def test_idempotency_key_is_normalized_for_duplicate_check_and_decision_engine() -> None:
    use_case = _use_case()

    await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="  ettn:INV-ETTN  "))

    assert use_case.import_history.calls == ["ettn:INV-ETTN"]
    assert use_case.decision_engine.commands[0].idempotency_key == "ettn:INV-ETTN"


@pytest.mark.asyncio
async def test_infrastructure_exceptions_are_translated_to_application_errors() -> None:
    use_case = _use_case(import_history=FailingImportHistory())

    with pytest.raises(ImportInvoiceInfrastructureError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert exc_info.value.safe_message == "History lookup unavailable."


@pytest.mark.asyncio
async def test_unexpected_decision_exception_is_translated_to_application_error() -> None:
    use_case = _use_case(decision_engine=FakeDecisionEngine(RuntimeError("transport details")))

    with pytest.raises(ImportInvoiceInfrastructureError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert exc_info.value.safe_message == "Decision Engine execution failed."


def test_import_invoice_dtos_are_immutable() -> None:
    command = ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN")
    result = ImportInvoiceResult(success=True, invoice_id="INV-ETTN", status="dry_run")

    with pytest.raises(FrozenInstanceError):
        command.dry_run = False
    with pytest.raises(FrozenInstanceError):
        result.status = "created"


def test_application_package_exports_import_invoice_use_case() -> None:
    assert app.application.ImportInvoiceUseCase is ImportInvoiceUseCase


class FakeImportHistory:
    def __init__(self, existing: ExistingInvoiceImport | None = None) -> None:
        self.existing = existing
        self.calls: list[str] = []

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        self.calls.append(idempotency_key)
        return self.existing


class FailingImportHistory:
    calls: list[str] = []

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        self.calls.append(idempotency_key)
        raise SafeInfrastructureError("History lookup unavailable.")


class SafeInfrastructureError(Exception):
    def __init__(self, safe_message: str) -> None:
        super().__init__("unsafe provider details")
        self.safe_message = safe_message


class FakeDecisionEngine:
    def __init__(self, result: DecisionResult | Exception | None = None) -> None:
        self.result = result or DecisionResult(
            success=True,
            invoice_id="INV-ETTN",
            workflow=WorkflowType.VENDOR_BILL,
            strategy=WorkflowType.VENDOR_BILL.value,
            status="dry_run",
        )
        self.commands: list[ImportInvoiceCommand] = []

    async def decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        self.commands.append(command)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class UseCaseFixture(ImportInvoiceUseCase):
    import_history: FakeImportHistory | FailingImportHistory
    decision_engine: FakeDecisionEngine


def _use_case(
    *,
    existing: ExistingInvoiceImport | None = None,
    import_history: FakeImportHistory | FailingImportHistory | None = None,
    decision_engine: FakeDecisionEngine | None = None,
) -> UseCaseFixture:
    history = import_history or FakeImportHistory(existing)
    engine = decision_engine or FakeDecisionEngine()
    use_case = UseCaseFixture(import_history=history, decision_engine=engine)  # type: ignore[arg-type]
    use_case.import_history = history
    use_case.decision_engine = engine
    return use_case


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="INV-UUID",
            ettn="INV-ETTN",
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
