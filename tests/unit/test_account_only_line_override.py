"""Explicit human account-only override for unmatched invoice lines (P0-PROD-07B).

Business policy: a supplier-provided product identifier does not mean ICT must create
an Odoo product master record. A human reviewer may explicitly mark one invoice line
as account-only, so it posts directly to a deterministic expense account instead of a
product. This must be an explicit, persisted human decision -- never inferred from an
unmatched product -- and the existing automatic fail-closed path (no product, no
override) must be completely unaffected.

Scenarios A-G below match the P0-PROD-07B specification:
  A. no override -> existing failure remains
  B. explicit override + valid account mapping -> account-only line builds
  C. explicit override + unresolvable account -> fail closed
  D. product resolves normally, no override -> unchanged
  E. persisted line resolution survives review read/write and is consumed by execution
  F. source supplier/product identifiers remain unchanged
  G. no automatic PRODUCT_NOT_FOUND -> expense conversion exists
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.application.execution import VendorBillExecutionStrategy
from app.application.execution.contracts import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionSourceInvoice,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.expense_mapping import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.application.workbench.dto import LineResolution
from app.application.workbench.exceptions import WorkbenchContractError
from app.billing import VendorBillBuilder, VendorBillBuildError, VendorBillLine
from app.billing.builder import validate_vendor_bill_inputs
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

EXPENSE_ACCOUNT_ID = 9101
TAX_ID = 401
SELLER_ITEM_CODE = "HBV000006MHLQ"


# --------------------------------------------------------------------------- builders


def _line(
    line_number: str,
    *,
    seller_item_code: str | None = None,
    quantity: Decimal = Decimal("1"),
    unit_price: Decimal = Decimal("50.00"),
) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description=f"Line {line_number}",
        seller_item_code=seller_item_code,
        quantity=quantity,
        unit_code="NIU",
        unit_price=unit_price,
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )


def _invoice(lines: list[InvoiceLine]) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="HD1202600096",
            invoice_uuid="uuid-1",
            ettn="uuid-1",
            issue_date=date(2026, 8, 1),
            currency_code="TRY",
        ),
        supplier=Party(name="D-Market", tax_number="0430367181"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("60.00")),
        lines=tuple(lines),
    )


def _partner(status: PartnerMatchStatus = PartnerMatchStatus.MATCHED) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=101 if matched else None,
        matched_by="tax_number" if matched else None,
        reason="matched" if matched else "unmatched",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _product_line(
    line_number: str,
    status: ProductMatchStatus,
    *,
    product_id: int | None = None,
    seller_item_code: str | None = None,
) -> InvoiceProductLineResult:
    return InvoiceProductLineResult(
        line_number=line_number,
        result=ProductMatchResult(
            status=status,
            line_number=line_number,
            product_id=product_id,
            default_code=None,
            barcode=None,
            seller_item_code=seller_item_code,
            matched_by="seller_item_code" if status is ProductMatchStatus.MATCHED else None,
            reason="matched" if status is ProductMatchStatus.MATCHED else "No Odoo product found.",
            candidate_count=1 if status is ProductMatchStatus.MATCHED else 0,
            confidence=Decimal("1.00") if status is ProductMatchStatus.MATCHED else None,
        ),
    )


def _products(line_results: list[InvoiceProductLineResult]) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(line_results=tuple(line_results))


def _taxes(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=tax_index,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=TAX_ID,
                    company_id=1,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="matched",
                    candidate_count=1,
                ),
            )
            for line in invoice.lines
            for tax_index, _tax in enumerate(line.taxes)
        )
    )


def _expense_match(
    status: OperatingExpenseMatchStatus = OperatingExpenseMatchStatus.MATCHED,
    *,
    expense_account_id: int | None = EXPENSE_ACCOUNT_ID,
) -> OperatingExpenseMatchResult:
    if status is not OperatingExpenseMatchStatus.MATCHED:
        return OperatingExpenseMatchResult(status=status, reason="not matched", candidate_count=0)
    return OperatingExpenseMatchResult(
        status=status,
        reason="Exact company and supplier partner operating-expense mapping.",
        candidate_count=1,
        mapping_id=7,
        company_id=1,
        vendor_partner_id=101,
        expense_account_id=expense_account_id,
        expense_category="OFFICE_SUPPLIES",
        matched_by="company_partner",
        confidence=Decimal("1.00"),
    )


# --------------------------------------------------------------------------- LineResolution DTO


def test_line_resolution_rejects_account_only_with_a_selected_product() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", selected_product_id=10, account_only=True)


def test_line_resolution_account_only_needs_no_product_id() -> None:
    resolution = LineResolution(line_number="1", account_only=True)
    assert resolution.selected_product_id is None
    assert resolution.account_only is True


def test_line_resolution_default_requires_positive_product_id() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1")


# ------------------------------------------------- LineResolution.expense_account_id (P0-PROD-08G)
# Domain-contract truth table A-G. Case C (account_only=True, expense_account_id=None)
# stays VALID at this layer on purpose -- legacy/vendor-wide account_only evidence
# persisted before this field existed must keep deserializing (see LineResolution's
# own docstring); the stricter "always require it for new submissions" rule lives at
# the REST/Studio ingestion boundary, not here.


def test_a_selected_product_only_is_valid() -> None:
    resolution = LineResolution(line_number="1", selected_product_id=10)
    assert resolution.expense_account_id is None


def test_b_account_only_with_explicit_expense_account_is_valid() -> None:
    resolution = LineResolution(line_number="1", account_only=True, expense_account_id=9001)
    assert resolution.selected_product_id is None
    assert resolution.expense_account_id == 9001


def test_c_account_only_without_expense_account_remains_valid_at_domain_layer() -> None:
    """Legacy compatibility: old persisted evidence has no expense_account_id at all."""
    resolution = LineResolution(line_number="1", account_only=True)
    assert resolution.expense_account_id is None


def test_d_expense_account_id_without_account_only_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", selected_product_id=10, expense_account_id=9001)


def test_e_selected_product_and_account_only_together_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", selected_product_id=10, account_only=True)


def test_f_selected_product_and_expense_account_id_together_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", selected_product_id=10, expense_account_id=9001)


def test_g_non_positive_expense_account_id_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", account_only=True, expense_account_id=0)
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", account_only=True, expense_account_id=-1)


# --------------------------------------------------------------------------- A. no override: unchanged failure


def test_a_unmatched_product_with_identifier_and_no_override_still_fails_closed() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(invoice, _partner(), product_match, _taxes(invoice), company_id=1)

    assert "Product mapping for line 1 is not matched." in exc_info.value.errors


def test_a_unmatched_product_fails_even_when_an_expense_mapping_would_have_resolved() -> None:
    """No implicit conversion: an available expense mapping never substitutes silently."""
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_expense_match=_expense_match(),  # present, but no override was requested
    )
    assert not result.is_valid
    assert "lines[0].product must be matched." in result.errors


# --------------------------------------------------------------------------- B. explicit override succeeds


def test_b_explicit_account_only_override_builds_account_line() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"1"}),
        account_only_expense_match=_expense_match(),
    )

    assert bill.invoice_lines == (
        VendorBillLine(
            product_id=None,
            account_id=EXPENSE_ACCOUNT_ID,
            quantity=Decimal("1"),
            uom=None,
            unit_price=Decimal("50.00"),
            tax_ids=(TAX_ID,),
            description="Line 1",
        ),
    )


# --------------------------------------------------------------------------- C. unresolvable account: fail closed


@pytest.mark.parametrize(
    "expense_match",
    [
        None,
        _expense_match(OperatingExpenseMatchStatus.NOT_FOUND),
        _expense_match(expense_account_id=0),
    ],
)
def test_c_explicit_override_with_unresolvable_account_fails_closed(expense_match) -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"1"}),
        account_only_expense_match=expense_match,
    )
    assert not result.is_valid
    assert "Explicit account-only line resolution requires a deterministic expense account mapping." in result.errors

    with pytest.raises(VendorBillBuildError):
        VendorBillBuilder().build(
            invoice,
            _partner(),
            product_match,
            _taxes(invoice),
            company_id=1,
            account_only_line_numbers=frozenset({"1"}),
            account_only_expense_match=expense_match,
        )


# --------------------------------------------------------------------------- D. product resolves normally: unchanged


def test_d_matched_product_is_unaffected_by_an_unused_account_only_expense_match() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.MATCHED, product_id=501)])

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_expense_match=_expense_match(),  # no line named in account_only_line_numbers
    )

    assert bill.invoice_lines[0].product_id == 501
    assert bill.invoice_lines[0].account_id is None


# --------------------------------------- Explicit per-line expense account (P0-PROD-08G)
# P. explicit wins over the legacy pinned account_only_expense_match
# Q. legacy vendor-wide account_only_expense_match still executes unchanged with no
#    explicit_account_only_accounts at all (already proven by test_b/test_d above --
#    neither passes explicit_account_only_accounts, and both still pass)


EXPLICIT_LINE_ACCOUNT_ID = 8801


def test_p_explicit_per_line_account_wins_over_legacy_vendor_wide_mapping() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"1"}),
        account_only_expense_match=_expense_match(),  # would resolve to EXPENSE_ACCOUNT_ID
        explicit_account_only_accounts={"1": EXPLICIT_LINE_ACCOUNT_ID},
    )

    assert bill.invoice_lines[0].account_id == EXPLICIT_LINE_ACCOUNT_ID
    assert bill.invoice_lines[0].account_id != EXPENSE_ACCOUNT_ID
    assert bill.invoice_lines[0].product_id is None


def test_p_explicit_per_line_account_works_with_no_legacy_mapping_at_all() -> None:
    """The whole point of P0-PROD-08G: D-Market gets no vendor-wide mapping."""
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"1"}),
        account_only_expense_match=None,  # no vendor-wide mapping exists
        explicit_account_only_accounts={"1": EXPLICIT_LINE_ACCOUNT_ID},
    )

    assert bill.invoice_lines[0].account_id == EXPLICIT_LINE_ACCOUNT_ID
    assert bill.invoice_lines[0].product_id is None


def test_s_mixed_invoice_product_plus_explicit_account_only_line() -> None:
    invoice = _invoice(
        [
            _line("1", seller_item_code="SKU-1"),
            _line("2", seller_item_code=SELLER_ITEM_CODE, unit_price=Decimal("30.00")),
        ]
    )
    product_match = _products(
        [
            _product_line("1", ProductMatchStatus.MATCHED, product_id=111),
            _product_line("2", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE),
        ]
    )

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"2"}),
        explicit_account_only_accounts={"2": EXPLICIT_LINE_ACCOUNT_ID},
    )

    line1, line2 = bill.invoice_lines
    assert line1.product_id == 111 and line1.account_id is None
    assert line2.product_id is None and line2.account_id == EXPLICIT_LINE_ACCOUNT_ID


def test_explicit_account_missing_for_one_of_two_account_only_lines_fails_closed() -> None:
    invoice = _invoice(
        [
            _line("1", seller_item_code=SELLER_ITEM_CODE),
            _line("2", seller_item_code="HBV999", unit_price=Decimal("10.00")),
        ]
    )
    product_match = _products(
        [
            _product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE),
            _product_line("2", ProductMatchStatus.NOT_FOUND, seller_item_code="HBV999"),
        ]
    )

    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"1", "2"}),
        account_only_expense_match=None,
        explicit_account_only_accounts={"1": EXPLICIT_LINE_ACCOUNT_ID},  # line 2 has none
    )
    assert not result.is_valid
    assert "Explicit account-only line resolution requires a deterministic expense account mapping." in result.errors


# --------------------------------------------------------------------------- Mixed invoice (requirement 7)


def test_mixed_invoice_builds_one_account_only_line_and_one_product_line() -> None:
    invoice = _invoice(
        [
            _line("1", seller_item_code=SELLER_ITEM_CODE),
            _line("2", seller_item_code="SKU-2", unit_price=Decimal("30.00")),
        ]
    )
    product_match = _products(
        [
            _product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE),
            _product_line("2", ProductMatchStatus.MATCHED, product_id=777),
        ]
    )

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"1"}),
        account_only_expense_match=_expense_match(),
    )

    assert len(bill.invoice_lines) == 2
    line_one, line_two = bill.invoice_lines
    assert line_one.product_id is None
    assert line_one.account_id == EXPENSE_ACCOUNT_ID
    assert line_two.product_id == 777
    assert line_two.account_id is None


# --------------------------------------------------------------------------- requirement 9: whole-invoice mode wins


def test_whole_invoice_operating_expense_mode_ignores_account_only_line_numbers() -> None:
    """An identifier-free invoice's existing automatic expense mode is untouched."""
    invoice = _invoice([_line("1")])  # no seller_item_code -> identifier-free
    product_match = _products([_product_line("1", ProductMatchStatus.INVALID_INPUT)])

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(expense_account_id=8001),
        account_only_line_numbers=frozenset({"1"}),
        account_only_expense_match=_expense_match(expense_account_id=EXPENSE_ACCOUNT_ID),
    )

    # The whole-invoice expense account wins, not the (unused) account-only match.
    assert bill.invoice_lines[0].account_id == 8001


# --------------------------------------------------------------------------- F. source evidence stays immutable


def test_f_seller_item_code_is_never_removed_by_an_account_only_override() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    VendorBillBuilder().build(
        invoice,
        _partner(),
        product_match,
        _taxes(invoice),
        company_id=1,
        account_only_line_numbers=frozenset({"1"}),
        account_only_expense_match=_expense_match(),
    )

    assert invoice.lines[0].seller_item_code == SELLER_ITEM_CODE


# --------------------------------------------------------------------------- E. execution consumes persisted decision


class _RecordingBuilder(VendorBillBuilder):
    def __init__(self) -> None:
        self.calls = 0
        self.last_account_only_line_numbers: frozenset[str] | None = None
        self.last_account_only_expense_match = None
        self.last_explicit_account_only_accounts = None

    def build(
        self,
        invoice,
        partner_match,
        product_match,
        tax_match,
        *,
        company_id=None,
        operating_expense_match=None,
        account_only_line_numbers: frozenset[str] = frozenset(),
        account_only_expense_match=None,
        explicit_account_only_accounts=None,
    ):
        self.calls += 1
        self.last_account_only_line_numbers = account_only_line_numbers
        self.last_account_only_expense_match = account_only_expense_match
        self.last_explicit_account_only_accounts = explicit_account_only_accounts
        from app.billing.dto import VendorBill

        return VendorBill(
            supplier_id=101,
            invoice_number="HD1202600096",
            invoice_date=date(2026, 8, 1),
            currency="TRY",
            external_uuid="uuid-1",
            reference="HD1202600096",
            company_id=1,
            invoice_lines=(
                VendorBillLine(
                    product_id=None,
                    account_id=EXPENSE_ACCOUNT_ID,
                    quantity=Decimal("1"),
                    uom=None,
                    unit_price=Decimal("50.00"),
                    tax_ids=(TAX_ID,),
                ),
            ),
        )


class _StaticReader:
    def __init__(self, source: ExecutionSourceInvoice) -> None:
        self._source = source

    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        return self._source


class _StubWriter:
    def __init__(self) -> None:
        self.commands = []

    async def write_vendor_bill(self, command):
        from app.application.dto import VendorBillWriteResult

        self.commands.append(command)
        return VendorBillWriteResult(
            status="dry_run", idempotency_key=command.idempotency_key, safe_message="dry run", success=True
        )


def _source(invoice: InternalInvoice, *, line_resolutions: tuple[LineResolution, ...] = ()) -> ExecutionSourceInvoice:
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])
    return ExecutionSourceInvoice(
        review_id="review-1",
        company_id=1,
        decision_version=2,
        source_invoice_id="uuid-1",
        invoice=invoice,
        partner_match=_partner(),
        product_match=product_match,
        tax_match=_taxes(invoice),
        account_only_expense_match=_expense_match(),
        line_resolutions=line_resolutions,
    )


def _step_request() -> ExecutionStepRequest:
    return ExecutionStepRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=1,
        decision_version=2,
        mode=ExecutionMode.EXECUTE,
        step=ExecutionStep(
            step_key="review-1:2:vendor_bill:workflow",
            step_type=ExecutionStepType.VENDOR_BILL,
            allocation_keys=(),
            sequence=1,
            execute_supported=True,
        ),
        approval=ExecutionApproval(approved_by="finance.lead"),
    )


def test_e_persisted_account_only_resolution_is_consumed_by_execution() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    source = _source(invoice, line_resolutions=(LineResolution(line_number="1", account_only=True),))
    builder = _RecordingBuilder()

    result = VendorBillExecutionStrategy(
        source_invoice_reader=_StaticReader(source),
        vendor_bill_builder=builder,
        vendor_bill_writer=_StubWriter(),
    ).execute(_step_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert builder.calls == 1
    assert builder.last_account_only_line_numbers == frozenset({"1"})
    assert builder.last_account_only_expense_match is not None
    assert builder.last_account_only_expense_match.status is OperatingExpenseMatchStatus.MATCHED


def test_e_no_line_resolution_never_triggers_account_only_mode() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    source = _source(invoice, line_resolutions=())
    builder = _RecordingBuilder()

    VendorBillExecutionStrategy(
        source_invoice_reader=_StaticReader(source),
        vendor_bill_builder=builder,
        vendor_bill_writer=_StubWriter(),
    ).execute(_step_request())

    assert builder.last_account_only_line_numbers == frozenset()


def test_e_a_non_account_only_line_resolution_does_not_trigger_account_only_mode() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    source = _source(invoice, line_resolutions=(LineResolution(line_number="1", selected_product_id=555),))
    builder = _RecordingBuilder()

    VendorBillExecutionStrategy(
        source_invoice_reader=_StaticReader(source),
        vendor_bill_builder=builder,
        vendor_bill_writer=_StubWriter(),
    ).execute(_step_request())

    assert builder.last_account_only_line_numbers == frozenset()


# --------------------------------------------------------------------------- G. no automatic expense conversion


def test_g_no_automatic_product_not_found_to_expense_conversion() -> None:
    """PRODUCT_NOT_FOUND alone -- with no explicit LineResolution.account_only -- never
    produces an account-only Vendor Bill, however favorable the available expense
    mapping is. Covers the same invariant end-to-end through the execution strategy."""
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    source = _source(invoice, line_resolutions=())  # no override submitted
    builder = VendorBillBuilder()  # the real builder, not a stub

    result = VendorBillExecutionStrategy(
        source_invoice_reader=_StaticReader(source),
        vendor_bill_builder=builder,
        vendor_bill_writer=_StubWriter(),
    ).execute(_step_request())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "vendor_bill_build_error"
