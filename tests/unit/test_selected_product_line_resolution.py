"""Explicit human-selected product mapping for an unmatched invoice line (P0-PROD-07D).

An operator's explicit ``LineResolution.selected_product_id`` is validated once, at
decision-acceptance time (never at Vendor Bill execution), then pinned by substituting
that one line's ``ProductMatchResult`` inside the already-persisted product-match
evidence. No new evidence field, no migration, no live Odoo lookup during execution --
see ``app.application.workbench.selected_product_resolution``.

Scenarios A-K below match the P0-PROD-07D specification:
  A. no override -> existing failure remains
  B. explicit valid selected_product_id -> selected product is used for that line
  C. selected product does not exist -> fails closed before execution
  D. selected product inactive -> fails closed
  E. selected product company-incompatible -> fails closed
  F. selected_product_id + account_only together -> rejected
  G. normal deterministic match, no override -> unchanged
  H. mixed invoice: deterministic + selected + account_only -> three correct outcomes
  I. source seller_item_code etc. remain unchanged
  J. execution performs no live product lookup
  K. persist/reload preserves the human selection
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution import VendorBillExecutionStrategy
from app.application.execution.contracts import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.workbench import (
    LineResolution,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewExecutionEvidence,
    ReviewItem,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
)
from app.application.workbench.exceptions import ReviewDecisionError, WorkbenchContractError
from app.application.workbench.selected_product_resolution import (
    HUMAN_SELECTED_MATCHED_BY,
    ResolutionProductRecord,
    apply_selected_product_resolutions,
)
from app.application.workflow import WorkflowType
from app.billing import VendorBillBuilder, VendorBillBuildError, VendorBillLine
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyExecutionSourceInvoiceReader, SqlAlchemyReviewRepository, SqlAlchemyUnitOfWork
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
SELLER_ITEM_CODE = "HBV000006MHLQ"
TAX_ID = 401
SELECTED_PRODUCT_ID = 501


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
        totals=MonetaryTotals(payable_amount=Decimal("50.00")),
        lines=tuple(lines),
    )


def _partner() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=101,
        matched_by="tax_number",
        reason="matched",
        candidate_count=1,
        confidence=Decimal("1.00"),
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
            matched_by="default_code" if status is ProductMatchStatus.MATCHED else None,
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
                    company_id=COMPANY_ID,
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


def _record(
    product_id: int = SELECTED_PRODUCT_ID,
    *,
    active: bool = True,
    company_id: int | None = COMPANY_ID,
) -> ResolutionProductRecord:
    return ResolutionProductRecord(
        id=product_id, name="Thermos", default_code="TH-1", barcode=None, active=active, company_id=company_id
    )


# --------------------------------------------------------------------------- A. no override: unchanged failure


def test_a_unmatched_product_with_no_override_still_fails_closed() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])

    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(invoice, _partner(), product_match, _taxes(invoice), company_id=COMPANY_ID)

    assert "Product mapping for line 1 is not matched." in exc_info.value.errors


# --------------------------------------------------------------------------- B. explicit valid selection


def test_b_explicit_selected_product_replaces_the_failed_match() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])
    resolution = LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID)

    updated = apply_selected_product_resolutions(
        product_match,
        line_resolutions=(resolution,),
        company_id=COMPANY_ID,
        products_by_id={SELECTED_PRODUCT_ID: _record()},
    )

    line_result = updated.line_results[0].result
    assert line_result.status is ProductMatchStatus.MATCHED
    assert line_result.product_id == SELECTED_PRODUCT_ID
    assert line_result.matched_by == HUMAN_SELECTED_MATCHED_BY

    bill = VendorBillBuilder().build(invoice, _partner(), updated, _taxes(invoice), company_id=COMPANY_ID)
    assert bill.invoice_lines == (
        VendorBillLine(
            product_id=SELECTED_PRODUCT_ID,
            quantity=Decimal("1"),
            unit_price=Decimal("50.00"),
            tax_ids=(TAX_ID,),
            description="Line 1",
        ),
    )


# --------------------------------------------------------------------------- C/D/E. fail closed


def test_c_selected_product_does_not_exist_fails_closed() -> None:
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND)])
    resolution = LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID)

    with pytest.raises(ReviewDecisionError, match="does not exist"):
        apply_selected_product_resolutions(
            product_match, line_resolutions=(resolution,), company_id=COMPANY_ID, products_by_id={}
        )


def test_d_selected_product_inactive_fails_closed() -> None:
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND)])
    resolution = LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID)

    with pytest.raises(ReviewDecisionError, match="inactive"):
        apply_selected_product_resolutions(
            product_match,
            line_resolutions=(resolution,),
            company_id=COMPANY_ID,
            products_by_id={SELECTED_PRODUCT_ID: _record(active=False)},
        )


def test_e_selected_product_company_incompatible_fails_closed() -> None:
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND)])
    resolution = LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID)

    with pytest.raises(ReviewDecisionError, match="different company"):
        apply_selected_product_resolutions(
            product_match,
            line_resolutions=(resolution,),
            company_id=COMPANY_ID,
            products_by_id={SELECTED_PRODUCT_ID: _record(company_id=999)},
        )


def test_company_none_on_product_is_compatible_with_any_company() -> None:
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND)])
    resolution = LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID)

    updated = apply_selected_product_resolutions(
        product_match,
        line_resolutions=(resolution,),
        company_id=COMPANY_ID,
        products_by_id={SELECTED_PRODUCT_ID: _record(company_id=None)},
    )
    assert updated.line_results[0].result.status is ProductMatchStatus.MATCHED


# --------------------------------------------------------------------------- F. mutual exclusivity


def test_f_selected_product_id_and_account_only_together_are_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID, account_only=True)


# --------------------------------------------------------------------------- G. deterministic match unchanged


def test_g_deterministic_match_without_override_is_unchanged() -> None:
    invoice = _invoice([_line("1", seller_item_code="SKU-1")])
    product_match = _products([_product_line("1", ProductMatchStatus.MATCHED, product_id=999)])

    updated = apply_selected_product_resolutions(
        product_match, line_resolutions=(), company_id=COMPANY_ID, products_by_id={}
    )
    assert updated is product_match  # identity-preserved no-op

    bill = VendorBillBuilder().build(invoice, _partner(), product_match, _taxes(invoice), company_id=COMPANY_ID)
    assert bill.invoice_lines[0].product_id == 999


# --------------------------------------------------------------------------- H. mixed invoice


def test_h_mixed_invoice_deterministic_selected_and_account_only() -> None:
    invoice = _invoice(
        [
            _line("1", seller_item_code="SKU-1"),
            _line("2", seller_item_code=SELLER_ITEM_CODE, unit_price=Decimal("30.00")),
            _line("3", seller_item_code="HBV999", unit_price=Decimal("10.00")),
        ]
    )
    product_match = _products(
        [
            _product_line("1", ProductMatchStatus.MATCHED, product_id=111),
            _product_line("2", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE),
            _product_line("3", ProductMatchStatus.NOT_FOUND, seller_item_code="HBV999"),
        ]
    )
    line_resolutions = (
        LineResolution(line_number="2", selected_product_id=SELECTED_PRODUCT_ID),
        LineResolution(line_number="3", account_only=True),
    )

    updated_product_match = apply_selected_product_resolutions(
        product_match,
        line_resolutions=line_resolutions,
        company_id=COMPANY_ID,
        products_by_id={SELECTED_PRODUCT_ID: _record()},
    )

    from app.application.expense_mapping import OperatingExpenseMatchResult, OperatingExpenseMatchStatus

    account_only_line_numbers = frozenset({"3"})
    account_only_expense_match = OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="mapped",
        candidate_count=1,
        mapping_id=1,
        company_id=COMPANY_ID,
        vendor_partner_id=101,
        expense_account_id=9001,
        expense_category="OFFICE_SUPPLIES",
        matched_by="company_partner",
        confidence=Decimal("1.00"),
    )

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        updated_product_match,
        _taxes(invoice),
        company_id=COMPANY_ID,
        account_only_line_numbers=account_only_line_numbers,
        account_only_expense_match=account_only_expense_match,
    )

    line1, line2, line3 = bill.invoice_lines
    assert line1.product_id == 111 and line1.account_id is None
    assert line2.product_id == SELECTED_PRODUCT_ID and line2.account_id is None
    assert line3.product_id is None and line3.account_id == 9001


# --------------------------------------------------------------------------- I. source evidence untouched


def test_i_source_invoice_line_identifiers_are_never_rewritten() -> None:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    product_match = _products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)])
    resolution = LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID)

    updated = apply_selected_product_resolutions(
        product_match,
        line_resolutions=(resolution,),
        company_id=COMPANY_ID,
        products_by_id={SELECTED_PRODUCT_ID: _record()},
    )

    # The invoice DTO itself is never passed to apply_selected_product_resolutions --
    # confirm its line identifiers still describe the *source*, not the selection.
    assert invoice.lines[0].seller_item_code == SELLER_ITEM_CODE
    # Even the derived match-result's own seller_item_code field (informational,
    # sourced from the invoice line) is carried through unchanged.
    assert updated.line_results[0].result.seller_item_code == SELLER_ITEM_CODE


# --------------------------------------------------------------------------- J. no live lookup during execution


def test_j_execution_strategy_performs_no_live_product_lookup() -> None:
    source = Path("app/application/execution/vendor_bill_strategy.py").read_text(encoding="utf-8")
    assert "find_products_by_ids" not in source
    assert "find_by_ids" not in source
    assert "OdooProductRepository" not in source
    assert "OdooSelectedProductReader" not in source


def test_j_builder_source_has_no_odoo_product_lookup() -> None:
    source = Path("app/billing/builder.py").read_text(encoding="utf-8")
    assert "find_products_by_ids" not in source
    assert "OdooProductRepository" not in source


# --------------------------------------------------------------------------- K. persist/reload round trip


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class _StubSelectedProductReader:
    def __init__(self, records: tuple[ResolutionProductRecord, ...]) -> None:
        self._by_id = {record.id: record for record in records}

    def find_products_by_ids(self, product_ids: tuple[int, ...]) -> tuple[ResolutionProductRecord, ...]:
        return tuple(self._by_id[product_id] for product_id in product_ids if product_id in self._by_id)


def _stage1_evidence() -> ReviewExecutionEvidence:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    return ReviewExecutionEvidence(
        review_id="review-1",
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id="uuid-1",
        invoice=invoice,
        partner_match=_partner(),
        product_match=_products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)]),
        tax_match=_taxes(invoice),
    )


def _decision_command() -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id="review-1",
        company_id=COMPANY_ID,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        line_resolutions=(LineResolution(line_number="1", selected_product_id=SELECTED_PRODUCT_ID),),
        decided_by="finance.user",
        idempotency_key="decision:selected-product",
    )


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="uuid-1",
        invoice_number="HD1202600096",
        supplier_tax_number="0430367181",
        supplier_name="D-Market",
        invoice_date=date(2026, 8, 1),
        currency="TRY",
        total_amount=Decimal("50.00"),
        workflow=WorkflowType.VENDOR_BILL,
        status=ReviewStatus.PENDING_REVIEW,
    )


def test_k_persist_and_reload_preserves_the_human_selection(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item(),
        company_id=COMPANY_ID,
        idempotency_key="review-key-1",
        evidence=_stage1_evidence(),
    )

    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=repository,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_StubSelectedProductReader((_record(),)),
    )
    acknowledgement = use_case.execute(_decision_command())
    assert acknowledgement.accepted is True

    reloaded = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id="review-1",
        company_id=COMPANY_ID,
        decision_version=2,
    )
    line_result = reloaded.product_match.line_results[0].result
    assert line_result.status is ProductMatchStatus.MATCHED
    assert line_result.product_id == SELECTED_PRODUCT_ID
    assert line_result.matched_by == HUMAN_SELECTED_MATCHED_BY
    # Stage-1's own raw source invoice line identifiers are untouched by the reload.
    assert reloaded.invoice.lines[0].seller_item_code == SELLER_ITEM_CODE
    assert any(r.selected_product_id == SELECTED_PRODUCT_ID for r in reloaded.line_resolutions)

    # And execution consumes exactly this pinned evidence -- no further lookup needed.
    class _NoWriteWriter:
        async def write_vendor_bill(self, command):
            from app.application.dto import VendorBillWriteResult

            return VendorBillWriteResult(
                status="dry_run", idempotency_key=command.idempotency_key, safe_message="ok", success=True
            )

    class _StaticReader:
        def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int):
            return reloaded

    result = VendorBillExecutionStrategy(
        source_invoice_reader=_StaticReader(),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=_NoWriteWriter(),
    ).execute(
        ExecutionStepRequest(
            execution_id="execution-1",
            review_id="review-1",
            company_id=COMPANY_ID,
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
    )
    assert result.status is ExecutionStepStatus.EXECUTED


def test_k_fail_closed_decision_never_persists_stage_two_evidence(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item(),
        company_id=COMPANY_ID,
        idempotency_key="review-key-1",
        evidence=_stage1_evidence(),
    )
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=repository,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_StubSelectedProductReader(()),  # the selected id will not resolve
    )

    with pytest.raises(ReviewDecisionError):
        use_case.execute(_decision_command())

    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0
