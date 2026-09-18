"""Zero-write Vendor Bill preview (P0-PROD-09B).

Proves: preview reuses the exact persisted Stage-2 accepted evidence and the exact
same VendorBillBuilder/idempotency-key machinery real execution uses, performs no
current-time business matching, exposes the exact future EXECUTE idempotency
identity, makes exactly one read-only Odoo call (currency resolution) and zero
writes, and works with every business-write gate closed (it never reads a gate at
all -- there is nothing in its composition or call path that could).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.application.execution import (
    AcceptedReviewDecision,
    ExecutionSourceInvoice,
    PreviewVendorBillRequest,
    PreviewVendorBillUseCase,
)
from app.application.execution.exceptions import (
    ExecutionPreviewCurrencyResolutionError,
    ExecutionPreviewUnsupportedWorkflowError,
    ExecutionSourceInvoiceNotFoundError,
)
from app.application.execution.planner import ExecutionPlanner
from app.application.workbench import ReviewDecisionType
from app.application.workbench.dto import LineResolution
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workflow import WorkflowType
from app.billing import VendorBillBuilder
from app.billing.exceptions import VendorBillBuildError
from app.domain.invoice import Discount, Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.vendor_bill_preview_currency_reader import OdooVendorBillPreviewCurrencyReader
from app.erp.write.exceptions import VendorBillWriteValidationError
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
REVIEW_ID = "review:9b13ad00-e89b-5e0e-8f0e-6de12037e199"
DECISION_ID = "review-decision:5bb3b401-208c-484c-801d-a569bb8d8f2b"
DECISION_VERSION = 4
D_MARKET_VAT = "2650179910"
EXPECTED_D_MARKET_IDEMPOTENCY_KEY = "vendor-bill-write:99e53cf2f9777e3435e250ede0327587adb64922398c9251a2217e141e9f42b4"


# --------------------------------------------------------------------------- fakes


class StaticAcceptedDecisionReader:
    def __init__(self, decision: AcceptedReviewDecision | None) -> None:
        self._decision = decision

    def get_accepted_decision(
        self, *, review_id: str, company_id: int, decision_version: int
    ) -> AcceptedReviewDecision:
        if self._decision is None:
            raise ReviewNotFoundError("Accepted review decision was not found.")
        return self._decision


class StaticSourceInvoiceReader:
    def __init__(self, source: ExecutionSourceInvoice | None) -> None:
        self._source = source
        self.calls: list[tuple[str, int, int]] = []

    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        self.calls.append((review_id, company_id, decision_version))
        if self._source is None:
            raise ExecutionSourceInvoiceNotFoundError("Execution source invoice evidence was not found.")
        return self._source


class StaticCurrencyReader:
    def __init__(self, *, currency_id: int = 31, error: Exception | None = None) -> None:
        self._currency_id = currency_id
        self._error = error
        self.calls: list[str] = []

    def resolve_vendor_bill_currency_id(self, currency_code: str) -> int:
        self.calls.append(currency_code)
        if self._error is not None:
            raise self._error
        return self._currency_id


class RecordingReadOnlyOdooClient:
    """Only ``search_read`` -- mirrors production res.currency lookup exactly."""

    def __init__(self, *, records: list[dict] | None = None) -> None:
        self._records = records if records is not None else [{"id": 31, "name": "TRY", "active": True}]
        self.calls: list[dict] = []

    async def search_read(self, *, model: str, domain, fields, limit: int = 20, offset: int = 0):
        self.calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit, "offset": offset})
        return self._records


# --------------------------------------------------------------------------- builders


def _d_market_invoice(*, line_number: str = "1") -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="HD12026000964604",
            invoice_uuid="00000000-0000-4000-8000-00000000d001",
            ettn="D-MARKET-ETTN-1",
            issue_date=date(2026, 9, 10),
            currency_code="TRY",
        ),
        supplier=Party(name="D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ", tax_number=D_MARKET_VAT),
        customer=Party(name="ICT Teknoloji", tax_number="1112223334"),
        totals=MonetaryTotals(
            tax_exclusive_amount=Decimal("563.51"),
            tax_inclusive_amount=Decimal("676.21"),
            payable_amount=Decimal("676.21"),
        ),
        lines=(
            InvoiceLine(
                line_number=line_number,
                description="Kraf Kesim Tablası A2 45X60 3002G",
                seller_item_code="HBV000006MHLQ",
                quantity=Decimal("1"),
                unit_price=Decimal("805.01"),
                discounts=(Discount(amount=Decimal("241.50")),),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _d_market_source(*, account_only: bool = True) -> ExecutionSourceInvoice:
    invoice = _d_market_invoice()
    line_resolutions = (
        (
            LineResolution(
                line_number="1",
                selected_product_id=None,
                account_only=True,
                expense_account_id=247,
            ),
        )
        if account_only
        else ()
    )
    return ExecutionSourceInvoice(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        source_invoice_id="D-MARKET-ETTN-1",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=448,
            matched_by="tax_number",
            reason="Unique supplier partner match by tax number.",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(
                InvoiceProductLineResult(
                    line_number="1",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.NOT_FOUND,
                        line_number="1",
                        product_id=None,
                        default_code=None,
                        barcode=None,
                        seller_item_code="HBV000006MHLQ",
                        matched_by=None,
                        reason="No matching product.",
                        candidate_count=0,
                        confidence=None,
                    ),
                ),
            )
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=34,
                        company_id=COMPANY_ID,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="matched",
                        candidate_count=1,
                    ),
                ),
            )
        ),
        line_resolutions=line_resolutions,
    )


def _d_market_decision() -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id=DECISION_ID,
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=None,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def _product_invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-2",
            invoice_uuid="ETTN-2",
            ettn="ETTN-2",
            issue_date=date(2026, 8, 1),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="ICT", tax_number="9876543210"),
        totals=MonetaryTotals(payable_amount=Decimal("120.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Widget",
                buyer_item_code="SKU-1",
                quantity=Decimal("2"),
                unit_price=Decimal("50.00"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )


def _product_source() -> ExecutionSourceInvoice:
    invoice = _product_invoice()
    return ExecutionSourceInvoice(
        review_id="review-product-1",
        company_id=COMPANY_ID,
        decision_version=1,
        source_invoice_id="ETTN-2",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=501,
            matched_by="tax_number",
            reason="matched",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(
                InvoiceProductLineResult(
                    line_number="1",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.MATCHED,
                        line_number="1",
                        product_id=601,
                        default_code="SKU-1",
                        barcode=None,
                        seller_item_code=None,
                        matched_by="default_code",
                        reason="matched",
                        candidate_count=1,
                        confidence=Decimal("1.00"),
                    ),
                ),
            )
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=701,
                        company_id=COMPANY_ID,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="matched",
                        candidate_count=1,
                    ),
                ),
            )
        ),
    )


def _product_decision() -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id="review-product-1",
        company_id=COMPANY_ID,
        decision_version=1,
        decision_id="decision-product-1",
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=None,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def _use_case(
    *,
    decision: AcceptedReviewDecision | None,
    source: ExecutionSourceInvoice | None,
    currency_reader=None,
) -> tuple[PreviewVendorBillUseCase, StaticSourceInvoiceReader, StaticCurrencyReader]:
    source_reader = StaticSourceInvoiceReader(source)
    currency = currency_reader or StaticCurrencyReader()
    use_case = PreviewVendorBillUseCase(
        accepted_decision_reader=StaticAcceptedDecisionReader(decision),
        source_invoice_reader=source_reader,
        execution_planner=ExecutionPlanner(),
        vendor_bill_builder=VendorBillBuilder(),
        currency_reader=currency,
    )
    return use_case, source_reader, currency


def _request(*, review_id: str = REVIEW_ID, decision_version: int = DECISION_VERSION) -> PreviewVendorBillRequest:
    return PreviewVendorBillRequest(review_id=review_id, company_id=COMPANY_ID, decision_version=decision_version)


# --------------------------------------------------------------------------- 1: D-Market account_only preview


def test_d_market_account_only_preview_reproduces_exact_pilot_economics() -> None:
    use_case, _, currency = _use_case(decision=_d_market_decision(), source=_d_market_source(account_only=True))

    preview = use_case.preview(_request())

    assert preview.review_id == REVIEW_ID
    assert preview.decision_id == DECISION_ID
    assert preview.decision_version == DECISION_VERSION
    assert preview.selected_workflow is WorkflowType.VENDOR_BILL
    assert preview.move_type == "in_invoice"
    assert preview.partner_id == 448
    assert preview.invoice_date == date(2026, 9, 10)
    assert preview.reference == "HD12026000964604"
    assert preview.currency_code == "TRY"
    assert preview.currency_id == 31
    assert currency.calls == ["TRY"]

    # 7: exact known D-Market totals
    assert preview.gross_source_amount == Decimal("805.01")
    assert preview.total_discount == Decimal("241.50")
    assert preview.preview_untaxed == Decimal("563.51")
    assert preview.preview_tax == Decimal("112.70")
    assert preview.preview_total == Decimal("676.21")

    assert len(preview.lines) == 1
    line = preview.lines[0]
    assert line.line_number == "1"
    assert line.quantity == Decimal("1")
    assert line.unit_price == Decimal("563.510000")
    assert line.account_id == 247
    assert line.product_id is None
    assert line.tax_ids == (34,)

    # 9: exact known D-Market idempotency identity
    assert preview.idempotency_key == EXPECTED_D_MARKET_IDEMPOTENCY_KEY


# --------------------------------------------------------------------------- 2: product-backed preview


def test_product_backed_vendor_bill_preview() -> None:
    use_case, _, _ = _use_case(decision=_product_decision(), source=_product_source())

    preview = use_case.preview(_request(review_id="review-product-1", decision_version=1))

    assert len(preview.lines) == 1
    line = preview.lines[0]
    assert line.product_id == 601
    assert line.account_id is None
    assert line.quantity == Decimal("2")
    assert line.unit_price == Decimal("50.00")
    assert line.tax_ids == (701,)
    assert preview.preview_untaxed == Decimal("100.00")
    assert preview.preview_tax == Decimal("20.00")
    assert preview.preview_total == Decimal("120.00")
    assert preview.gross_source_amount == Decimal("100.00")
    assert preview.total_discount == Decimal("0")


# --------------------------------------------------------------------------- 3: mixed account_only/product invoice


def _mixed_invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-MIX-1",
            invoice_uuid="ETTN-MIX-1",
            ettn="ETTN-MIX-1",
            issue_date=date(2026, 8, 5),
            currency_code="TRY",
        ),
        supplier=Party(name="Mixed Supplier", tax_number="5556667778"),
        customer=Party(name="ICT", tax_number="9876543210"),
        totals=MonetaryTotals(payable_amount=Decimal("240.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Matched product",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
            InvoiceLine(
                line_number="2",
                description="Unmatched -> account_only",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )


def _mixed_source() -> ExecutionSourceInvoice:
    invoice = _mixed_invoice()
    return ExecutionSourceInvoice(
        review_id="review-mixed-1",
        company_id=COMPANY_ID,
        decision_version=1,
        source_invoice_id="ETTN-MIX-1",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=502,
            matched_by="tax_number",
            reason="matched",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(
                InvoiceProductLineResult(
                    line_number="1",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.MATCHED,
                        line_number="1",
                        product_id=611,
                        default_code="SKU-M1",
                        barcode=None,
                        seller_item_code=None,
                        matched_by="default_code",
                        reason="matched",
                        candidate_count=1,
                        confidence=Decimal("1.00"),
                    ),
                ),
                InvoiceProductLineResult(
                    line_number="2",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.NOT_FOUND,
                        line_number="2",
                        product_id=None,
                        default_code=None,
                        barcode=None,
                        seller_item_code="UNMATCHED-1",
                        matched_by=None,
                        reason="No matching product.",
                        candidate_count=0,
                        confidence=None,
                    ),
                ),
            )
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=701,
                        company_id=COMPANY_ID,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="matched",
                        candidate_count=1,
                    ),
                ),
                InvoiceTaxLineResult(
                    line_number="2",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=701,
                        company_id=COMPANY_ID,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="matched",
                        candidate_count=1,
                    ),
                ),
            )
        ),
        line_resolutions=(
            LineResolution(line_number="2", selected_product_id=None, account_only=True, expense_account_id=270),
        ),
    )


def _mixed_decision() -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id="review-mixed-1",
        company_id=COMPANY_ID,
        decision_version=1,
        decision_id="decision-mixed-1",
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=None,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def test_mixed_account_only_and_product_invoice_preview() -> None:
    use_case, _, _ = _use_case(decision=_mixed_decision(), source=_mixed_source())

    preview = use_case.preview(_request(review_id="review-mixed-1", decision_version=1))

    assert len(preview.lines) == 2
    product_line, account_line = preview.lines
    assert product_line.product_id == 611
    assert product_line.account_id is None
    assert account_line.product_id is None
    assert account_line.account_id == 270
    # 6: multi-line totals
    assert preview.preview_untaxed == Decimal("200.00")
    assert preview.preview_tax == Decimal("40.00")
    assert preview.preview_total == Decimal("240.00")


# --------------------------------------------------------------------------- 4/5: discount preservation


def test_discount_preservation_matches_p0_prod_08l_economics() -> None:
    use_case, _, _ = _use_case(decision=_d_market_decision(), source=_d_market_source())
    preview = use_case.preview(_request())
    assert preview.gross_source_amount - preview.total_discount == preview.preview_untaxed


def test_no_discount_invoice_preview_is_unaffected() -> None:
    use_case, _, _ = _use_case(decision=_product_decision(), source=_product_source())
    preview = use_case.preview(_request(review_id="review-product-1", decision_version=1))
    assert preview.total_discount == Decimal("0")
    assert preview.gross_source_amount == preview.preview_untaxed


# --------------------------------------------------------------------------- 7 covered above; 8: currency resolution


def test_currency_resolution_through_real_read_only_adapter() -> None:
    client = RecordingReadOnlyOdooClient(records=[{"id": 31, "name": "TRY", "active": True}])
    adapter = OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0)
    reader = OdooVendorBillPreviewCurrencyReader(adapter=adapter)
    use_case, _, _ = _use_case(decision=_d_market_decision(), source=_d_market_source(), currency_reader=reader)

    preview = use_case.preview(_request())

    assert preview.currency_id == 31
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "res.currency"
    assert not hasattr(client, "create_account_move")
    assert not hasattr(client, "create_res_partner")


# --------------------------------------------------------------------------- 10/11/12: missing evidence preconditions


def test_missing_accepted_decision_fails_closed() -> None:
    use_case, _, _ = _use_case(decision=None, source=_d_market_source())
    with pytest.raises(ReviewNotFoundError):
        use_case.preview(_request())


def test_missing_stage2_evidence_fails_closed() -> None:
    use_case, _, _ = _use_case(decision=_d_market_decision(), source=None)
    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        use_case.preview(_request())


def test_unsupported_selected_workflow_fails_closed() -> None:
    decision = AcceptedReviewDecision(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id=DECISION_ID,
        selected_workflow=WorkflowType.MANUAL_REVIEW
        if hasattr(WorkflowType, "MANUAL_REVIEW")
        else WorkflowType.VENDOR_BILL,
        business_context_allocations=None,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )
    if decision.selected_workflow is WorkflowType.VENDOR_BILL:
        pytest.skip("WorkflowType has no non-VENDOR_BILL, non-quotation value available for this fixture.")
    use_case, _, _ = _use_case(decision=decision, source=_d_market_source())
    with pytest.raises(ExecutionPreviewUnsupportedWorkflowError):
        use_case.preview(_request())


# --------------------------------------------------------------------------- 13: invalid/inactive/ambiguous currency


def test_missing_currency_fails_closed() -> None:
    error = VendorBillWriteValidationError("Vendor Bill currency must resolve to exactly one Odoo currency.")
    use_case, _, _ = _use_case(
        decision=_d_market_decision(),
        source=_d_market_source(),
        currency_reader=StaticCurrencyReader(error=error),
    )
    # The ERP-layer currency error is translated to a pure application-layer exception
    # inside the use case -- the router (and every other caller) never needs to depend
    # on any app.erp exception type to handle this failure (see the architecture
    # boundary test in test_workbench_routes.py).
    with pytest.raises(ExecutionPreviewCurrencyResolutionError):
        use_case.preview(_request())


def test_inactive_currency_fails_closed_through_real_adapter() -> None:
    client = RecordingReadOnlyOdooClient(records=[{"id": 31, "name": "TRY", "active": False}])
    adapter = OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0)
    reader = OdooVendorBillPreviewCurrencyReader(adapter=adapter)
    use_case, _, _ = _use_case(decision=_d_market_decision(), source=_d_market_source(), currency_reader=reader)
    with pytest.raises(ExecutionPreviewCurrencyResolutionError):
        use_case.preview(_request())


def test_ambiguous_currency_fails_closed_through_real_adapter() -> None:
    client = RecordingReadOnlyOdooClient(
        records=[
            {"id": 31, "name": "TRY", "active": True},
            {"id": 32, "name": "TRY", "active": True},
        ]
    )
    adapter = OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0)
    reader = OdooVendorBillPreviewCurrencyReader(adapter=adapter)
    use_case, _, _ = _use_case(decision=_d_market_decision(), source=_d_market_source(), currency_reader=reader)
    with pytest.raises(ExecutionPreviewCurrencyResolutionError):
        use_case.preview(_request())


# -------------------------------------------------------------- 14/15: account_only / selected_product pinning


def test_account_only_uses_pinned_expense_account_never_recomputed() -> None:
    use_case, _, _ = _use_case(decision=_d_market_decision(), source=_d_market_source(account_only=True))
    preview = use_case.preview(_request())
    assert preview.lines[0].account_id == 247


def test_account_only_line_missing_pinned_account_fails_at_build_not_silently() -> None:
    source = _d_market_source(account_only=False)
    use_case, _, _ = _use_case(decision=_d_market_decision(), source=source)
    with pytest.raises(VendorBillBuildError):
        use_case.preview(_request())


def test_selected_product_uses_pinned_product_never_recomputed() -> None:
    use_case, _, _ = _use_case(decision=_product_decision(), source=_product_source())
    preview = use_case.preview(_request(review_id="review-product-1", decision_version=1))
    assert preview.lines[0].product_id == 601


# -------------------------------------------------------------- 16/17/18: gate-independence and zero writes


def test_preview_use_case_never_references_any_write_gate_setting() -> None:
    """Structural proof, not behavioral: PreviewVendorBillUseCase's constructor has no
    parameter shaped like a write policy/gate at all -- there is nothing to check."""

    import inspect

    signature = inspect.signature(PreviewVendorBillUseCase.__init__)
    param_names = set(signature.parameters) - {"self"}
    assert param_names == {
        "accepted_decision_reader",
        "source_invoice_reader",
        "execution_planner",
        "vendor_bill_builder",
        "currency_reader",
    }


def test_preview_currency_reader_type_has_no_write_method() -> None:
    """OdooVendorBillPreviewCurrencyReader is built on OdooReadOnlyAdapter, which has
    no create/write/unlink method at all -- this is a structural, not boolean, guarantee."""

    reader_methods = {name for name in dir(OdooVendorBillPreviewCurrencyReader) if not name.startswith("_")}
    assert reader_methods == {"resolve_vendor_bill_currency_id"}
    adapter_methods = {name for name in dir(OdooReadOnlyAdapter) if not name.startswith("_")}
    for forbidden in ("create", "write", "unlink", "create_res_partner", "create_account_move"):
        assert forbidden not in adapter_methods


def test_zero_odoo_writes_only_one_search_read_call() -> None:
    client = RecordingReadOnlyOdooClient()
    adapter = OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0)
    reader = OdooVendorBillPreviewCurrencyReader(adapter=adapter)
    use_case, source_reader, _ = _use_case(
        decision=_d_market_decision(), source=_d_market_source(), currency_reader=reader
    )

    use_case.preview(_request())

    assert len(client.calls) == 1  # exactly one read-only Odoo call: currency resolution
    assert len(source_reader.calls) == 1  # exactly one read of persisted Stage-2 evidence


# --------------------------------------------------------------------------- 19: repeated preview is stable, no state


def test_repeated_preview_returns_equivalent_output_and_creates_no_state() -> None:
    use_case, source_reader, currency = _use_case(decision=_d_market_decision(), source=_d_market_source())

    first = use_case.preview(_request())
    second = use_case.preview(_request())

    assert first == second
    assert len(source_reader.calls) == 2  # read twice, nothing cached/mutated between calls
    assert currency.calls == ["TRY", "TRY"]


# --------------------------------------------------------------------------- 20: no retirement trigger


def test_preview_never_touches_one_off_vendor_retirement() -> None:
    """PreviewVendorBillUseCase holds no OneOffVendorRetirementTrigger/retirement writer
    dependency at all -- there is no code path by which preview could advance or read
    ONE_OFF_VENDOR retirement state."""

    import inspect

    source = inspect.getsource(PreviewVendorBillUseCase)
    assert "retirement" not in source.lower()
    assert "OneOffVendor" not in source
