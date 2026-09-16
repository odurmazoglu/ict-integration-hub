"""Explicit human-selected per-line expense account for an account-only invoice line
(P0-PROD-08G).

An operator's explicit ``LineResolution.expense_account_id`` is validated once, at
decision-acceptance time (never at Vendor Bill execution), against a minimal Odoo
``account.account`` snapshot -- then persisted verbatim as part of ``LineResolution``
itself (no new evidence field, no migration) and consumed by ``VendorBillBuilder``
with priority over the legacy whole-vendor ``OperatingExpenseMappingRecord`` fallback.

Scenarios (matching the P0-PROD-08G specification):
  K. selected account does not exist -> fails closed before execution
  L. selected account belongs to a different company -> fails closed
  M. valid account -> decision accepted
  N. exact account is pinned into execution evidence, byte-for-byte
  O. execution performs no live account lookup/recomputation
  T/U. PRODUCT_NOT_FOUND never automatically becomes account_only or gets an account
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
from app.application.workbench.exceptions import ReviewDecisionError
from app.application.workbench.selected_expense_account_resolution import (
    ResolutionAccountRecord,
    selected_expense_account_ids,
    validate_selected_expense_accounts,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.billing import VendorBillBuilder
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
from app.persistence import SqlAlchemyExecutionSourceInvoiceReader, SqlAlchemyReviewRepository
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
SELLER_ITEM_CODE = "HBV000006MHLQ"
TAX_ID = 401
EXPENSE_ACCOUNT_ID = 8801


# --------------------------------------------------------------------------- builders


def _line(
    line_number: str,
    *,
    seller_item_code: str | None = None,
    quantity: Decimal = Decimal("1"),
    unit_price: Decimal = Decimal("805.01"),
) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description=f"Line {line_number}",
        seller_item_code=seller_item_code,
        quantity=quantity,
        unit_code="C62",
        unit_price=unit_price,
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )


def _invoice(lines: list[InvoiceLine]) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="HD12026000964604",
            invoice_uuid="uuid-dmarket",
            ettn="uuid-dmarket",
            issue_date=date(2026, 9, 10),
            currency_code="TRY",
        ),
        supplier=Party(name="D-MARKET ELEKTRONIK HIZMETLER VE TICARET A.S.", tax_number="2650179910"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("805.01")),
        lines=tuple(lines),
    )


def _partner() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=201,
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


def _account_record(
    account_id: int = EXPENSE_ACCOUNT_ID,
    *,
    company_ids: tuple[int, ...] = (COMPANY_ID,),
) -> ResolutionAccountRecord:
    return ResolutionAccountRecord(id=account_id, company_ids=company_ids)


# --------------------------------------------------------------------- pure validation


def test_selected_expense_account_ids_deduplicates_and_skips_none() -> None:
    resolutions = (
        LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID),
        LineResolution(line_number="2", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID),
        LineResolution(line_number="3", selected_product_id=800),
    )
    assert selected_expense_account_ids(resolutions) == (EXPENSE_ACCOUNT_ID,)


def test_m_valid_account_passes_validation() -> None:
    resolution = LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID)
    validate_selected_expense_accounts(
        line_resolutions=(resolution,),
        company_id=COMPANY_ID,
        accounts_by_id={EXPENSE_ACCOUNT_ID: _account_record()},
    )  # does not raise


def test_k_selected_account_does_not_exist_fails_closed() -> None:
    resolution = LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID)
    with pytest.raises(ReviewDecisionError, match="does not exist"):
        validate_selected_expense_accounts(line_resolutions=(resolution,), company_id=COMPANY_ID, accounts_by_id={})


def test_l_selected_account_wrong_company_fails_closed() -> None:
    resolution = LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID)
    with pytest.raises(ReviewDecisionError, match="different company"):
        validate_selected_expense_accounts(
            line_resolutions=(resolution,),
            company_id=COMPANY_ID,
            accounts_by_id={EXPENSE_ACCOUNT_ID: _account_record(company_ids=(999,))},
        )


def test_account_shared_across_multiple_companies_is_compatible() -> None:
    resolution = LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID)
    validate_selected_expense_accounts(
        line_resolutions=(resolution,),
        company_id=COMPANY_ID,
        accounts_by_id={EXPENSE_ACCOUNT_ID: _account_record(company_ids=(COMPANY_ID, 999))},
    )  # does not raise


# ----------------------------------------------------------------- O. no live lookup


def test_o_execution_strategy_performs_no_live_account_lookup() -> None:
    source = Path("app/application/execution/vendor_bill_strategy.py").read_text(encoding="utf-8")
    assert "find_accounts_by_ids" not in source
    assert "OdooSelectedAccountReader" not in source


def test_o_builder_source_has_no_odoo_account_lookup() -> None:
    source = Path("app/billing/builder.py").read_text(encoding="utf-8")
    assert "find_accounts_by_ids" not in source
    assert "OdooSelectedAccountReader" not in source


# ------------------------------------------------------- T/U. no automatic inference


def test_t_review_item_product_not_found_reason_never_implies_account_only() -> None:
    """PRODUCT_NOT_FOUND is a classification fact about the review, entirely separate
    from LineResolution -- nothing maps one to the other automatically."""
    review_item = ReviewItem(
        review_id="review-1",
        invoice_id="uuid-dmarket",
        invoice_number="HD12026000964604",
        supplier_tax_number="2650179910",
        supplier_name="D-Market",
        invoice_date=date(2026, 9, 10),
        currency="TRY",
        total_amount=Decimal("805.01"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not found.",
                line_number="1",
                candidate_count=0,
                source="rule_engine",
                details=(),
            ),
        ),
    )
    # No LineResolution is derivable from review_reasons alone -- the only way to get
    # one is an explicit command.line_resolutions entry, never constructed from this.
    assert review_item.review_reasons[0].code is ManualReviewReasonCode.PRODUCT_NOT_FOUND


def test_u_empty_line_resolutions_never_synthesizes_an_expense_account() -> None:
    assert selected_expense_account_ids(()) == ()


# --------------------------------------------------------------------- N. persist/reload/execute round trip


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


class _StubSelectedAccountReader:
    def __init__(self, records: tuple[ResolutionAccountRecord, ...]) -> None:
        self._by_id = {record.id: record for record in records}

    def find_accounts_by_ids(self, account_ids: tuple[int, ...]) -> tuple[ResolutionAccountRecord, ...]:
        return tuple(self._by_id[account_id] for account_id in account_ids if account_id in self._by_id)


def _stage1_evidence() -> ReviewExecutionEvidence:
    invoice = _invoice([_line("1", seller_item_code=SELLER_ITEM_CODE)])
    return ReviewExecutionEvidence(
        review_id="review-dmarket",
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id="uuid-dmarket",
        invoice=invoice,
        partner_match=_partner(),
        product_match=_products([_product_line("1", ProductMatchStatus.NOT_FOUND, seller_item_code=SELLER_ITEM_CODE)]),
        tax_match=_taxes(invoice),
    )


def _decision_command() -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id="review-dmarket",
        company_id=COMPANY_ID,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        line_resolutions=(LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID),),
        decided_by="finance.user",
        idempotency_key="decision:expense-account",
    )


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-dmarket",
        invoice_id="uuid-dmarket",
        invoice_number="HD12026000964604",
        supplier_tax_number="2650179910",
        supplier_name="D-Market",
        invoice_date=date(2026, 9, 10),
        currency="TRY",
        total_amount=Decimal("805.01"),
        workflow=WorkflowType.VENDOR_BILL,
        status=ReviewStatus.PENDING_REVIEW,
    )


def test_n_persist_and_reload_pins_the_exact_expense_account(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item(),
        company_id=COMPANY_ID,
        idempotency_key="review-key-dmarket",
        evidence=_stage1_evidence(),
    )

    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=repository,
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_account_reader=_StubSelectedAccountReader((_account_record(),)),
    )
    acknowledgement = use_case.execute(_decision_command())
    assert acknowledgement.accepted is True

    reloaded = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id="review-dmarket",
        company_id=COMPANY_ID,
        decision_version=2,
    )
    resolution = reloaded.line_resolutions[0]
    assert resolution.account_only is True
    assert resolution.expense_account_id == EXPENSE_ACCOUNT_ID
    # Stage-1's own raw source invoice line identifiers are untouched.
    assert reloaded.invoice.lines[0].seller_item_code == SELLER_ITEM_CODE
    assert reloaded.invoice.lines[0].description == "Line 1"

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
            execution_id="execution-dmarket",
            review_id="review-dmarket",
            company_id=COMPANY_ID,
            decision_version=2,
            mode=ExecutionMode.EXECUTE,
            step=ExecutionStep(
                step_key="review-dmarket:2:vendor_bill:workflow",
                step_type=ExecutionStepType.VENDOR_BILL,
                allocation_keys=(),
                sequence=1,
                execute_supported=True,
            ),
            approval=ExecutionApproval(approved_by="finance.lead"),
        )
    )
    assert result.status is ExecutionStepStatus.EXECUTED


def test_k_fail_closed_decision_never_persists_when_account_missing(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item(),
        company_id=COMPANY_ID,
        idempotency_key="review-key-dmarket",
        evidence=_stage1_evidence(),
    )
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=repository,
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_account_reader=_StubSelectedAccountReader(()),  # the account will not resolve
    )

    with pytest.raises(ReviewDecisionError):
        use_case.execute(_decision_command())

    assert session.query(WorkbenchReviewDecision).count() == 0


def test_decision_fails_closed_when_no_account_reader_is_configured(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item(),
        company_id=COMPANY_ID,
        idempotency_key="review-key-dmarket",
        evidence=_stage1_evidence(),
    )
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=repository,
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        # selected_account_reader intentionally omitted
    )

    with pytest.raises(ReviewDecisionError, match="not configured"):
        use_case.execute(_decision_command())
