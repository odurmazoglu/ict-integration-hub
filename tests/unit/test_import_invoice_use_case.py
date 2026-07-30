from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

import app.application
from app.application.commands import ImportInvoiceCommand, VendorBillWriteCommand
from app.application.dto import ExistingInvoiceImport, ImportInvoiceResult, VendorBillWriteResult
from app.application.use_cases import (
    ImportInvoiceInfrastructureError,
    ImportInvoiceUseCase,
    ImportInvoiceValidationError,
)
from app.billing import VendorBillBuilder, VendorBillBuildError
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)


@pytest.mark.asyncio
async def test_import_invoice_orchestrates_vendor_bill_path() -> None:
    use_case = _use_case()
    command = ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)

    result = await use_case.execute(command)

    assert result.success is True
    assert result.invoice_id == "INV-ETTN"
    assert result.status == "dry_run"
    assert result.vendor_bill_id is None
    assert result.errors == ()
    assert result.duration >= 0
    assert use_case.import_history.calls == ["ettn:INV-ETTN"]
    assert use_case.partner_matcher.calls == [("INV-1", 7)]
    assert use_case.product_matcher.calls == [("INV-1", 7)]
    assert use_case.tax_mapper.calls == [("INV-1", 7)]
    assert len(use_case.vendor_bill_writer.commands) == 1
    write_command = use_case.vendor_bill_writer.commands[0]
    assert isinstance(write_command, VendorBillWriteCommand)
    assert write_command.idempotency_key == "ettn:INV-ETTN"
    assert write_command.dry_run is True
    assert write_command.vendor_bill.invoice_number == "INV-1"


@pytest.mark.asyncio
async def test_duplicate_import_short_circuits_matching_building_and_writing() -> None:
    use_case = _use_case(existing=ExistingInvoiceImport(invoice_id="INV-ETTN", vendor_bill_id=42))

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result == ImportInvoiceResult(
        success=True,
        invoice_id="INV-ETTN",
        status="already_imported",
        vendor_bill_id=42,
        warnings=("Invoice was already imported.",),
        duration=result.duration,
    )
    assert use_case.partner_matcher.calls == []
    assert use_case.product_matcher.calls == []
    assert use_case.tax_mapper.calls == []
    assert use_case.vendor_bill_writer.commands == []


@pytest.mark.asyncio
async def test_writer_existing_result_is_returned_without_erp_model_leakage() -> None:
    writer = FakeVendorBillWriter(
        VendorBillWriteResult(
            status="existing",
            idempotency_key="ettn:INV-ETTN",
            external_id=99,
            external_model="account.move",
            safe_message="Existing draft found.",
        )
    )
    use_case = _use_case(writer=writer)

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result.success is True
    assert result.status == "already_exists"
    assert result.vendor_bill_id == 99
    assert result.warnings == ("Existing draft found.",)
    assert not hasattr(result, "external_model")


@pytest.mark.asyncio
async def test_writer_failed_result_is_returned_as_safe_failure_result() -> None:
    writer = FakeVendorBillWriter(
        VendorBillWriteResult(
            status="failed",
            idempotency_key="ettn:INV-ETTN",
            safe_message="Vendor Bill write failed safely.",
        )
    )
    use_case = _use_case(writer=writer)

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
async def test_idempotency_key_is_normalized_for_duplicate_check_and_writer() -> None:
    use_case = _use_case()

    await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="  ettn:INV-ETTN  "))

    assert use_case.import_history.calls == ["ettn:INV-ETTN"]
    assert use_case.vendor_bill_writer.commands[0].idempotency_key == "ettn:INV-ETTN"


@pytest.mark.asyncio
async def test_infrastructure_exceptions_are_translated_to_application_errors() -> None:
    use_case = _use_case(import_history=FailingImportHistory())

    with pytest.raises(ImportInvoiceInfrastructureError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert exc_info.value.safe_message == "History lookup unavailable."


@pytest.mark.asyncio
async def test_domain_build_errors_propagate() -> None:
    use_case = _use_case(product_matcher=FakeProductMatcher(_product_match(ProductMatchStatus.NOT_FOUND)))

    with pytest.raises(VendorBillBuildError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert "Product mapping for line 1 is not matched." in exc_info.value.safe_message
    assert use_case.vendor_bill_writer.commands == []


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


class FakePartnerMatcher:
    def __init__(self, result: PartnerMatchResult | None = None) -> None:
        self.result = result or _partner_match()
        self.calls: list[tuple[str, int | None]] = []

    def match_supplier(self, invoice: InternalInvoice, *, company_id: int | None = None) -> PartnerMatchResult:
        self.calls.append((invoice.header.invoice_number, company_id))
        return self.result


class FakeProductMatcher:
    def __init__(self, result: InvoiceProductMatchResult | None = None) -> None:
        self.result = result or _product_match(ProductMatchStatus.MATCHED)
        self.calls: list[tuple[str, int | None]] = []

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceProductMatchResult:
        self.calls.append((invoice.header.invoice_number, company_id))
        return self.result


class FakeTaxMapper:
    def __init__(self, result: InvoiceTaxMappingResult | None = None) -> None:
        self.result = result or _tax_match()
        self.calls: list[tuple[str, int | None]] = []

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceTaxMappingResult:
        self.calls.append((invoice.header.invoice_number, company_id))
        return self.result


class FakeVendorBillWriter:
    def __init__(self, result: VendorBillWriteResult | None = None) -> None:
        self.result = result or VendorBillWriteResult(status="dry_run", idempotency_key="ettn:INV-ETTN")
        self.commands: list[VendorBillWriteCommand] = []

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        self.commands.append(command)
        return self.result


class UseCaseFixture(ImportInvoiceUseCase):
    import_history: FakeImportHistory | FailingImportHistory
    partner_matcher: FakePartnerMatcher
    product_matcher: FakeProductMatcher
    tax_mapper: FakeTaxMapper
    vendor_bill_writer: FakeVendorBillWriter


def _use_case(
    *,
    existing: ExistingInvoiceImport | None = None,
    import_history: FakeImportHistory | FailingImportHistory | None = None,
    partner_matcher: FakePartnerMatcher | None = None,
    product_matcher: FakeProductMatcher | None = None,
    tax_mapper: FakeTaxMapper | None = None,
    writer: FakeVendorBillWriter | None = None,
) -> UseCaseFixture:
    history = import_history or FakeImportHistory(existing)
    partner = partner_matcher or FakePartnerMatcher()
    product = product_matcher or FakeProductMatcher()
    tax = tax_mapper or FakeTaxMapper()
    vendor_bill_writer = writer or FakeVendorBillWriter()
    use_case = UseCaseFixture(
        import_history=history,
        partner_matcher=partner,
        product_matcher=product,
        tax_mapper=tax,
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=vendor_bill_writer,
    )
    use_case.import_history = history
    use_case.partner_matcher = partner
    use_case.product_matcher = product
    use_case.tax_mapper = tax
    use_case.vendor_bill_writer = vendor_bill_writer
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
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )


def _partner_match() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=10,
        matched_by="tax_number",
        reason="Unique supplier partner match.",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _product_match(status: ProductMatchStatus) -> InvoiceProductMatchResult:
    product_id = 20 if status is ProductMatchStatus.MATCHED else None
    return InvoiceProductMatchResult(
        line_results=(
            InvoiceProductLineResult(
                line_number="1",
                result=ProductMatchResult(
                    status=status,
                    line_number="1",
                    product_id=product_id,
                    default_code="SKU-1",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code" if product_id is not None else None,
                    reason="Product match result.",
                    candidate_count=1 if product_id is not None else 0,
                    confidence=Decimal("1.00") if product_id is not None else None,
                ),
            ),
        )
    )


def _tax_match() -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=30,
                    company_id=7,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="Exact tax match.",
                    candidate_count=1,
                ),
            ),
        )
    )
