"""P0-PROD-19F: review detail exposes the effective (accepted) state without rewriting history.

Accepting a decision advances the review version, while Stage-1 matching evidence stays
keyed by the version the decision was accepted against. Review detail used to read
Stage-1 at the post-decision version, so every line's ``product_match`` came back
``null`` after acceptance. These tests pin the corrected read model against real
SQLAlchemy persistence and the real Stage-2 reader execution itself uses:

* pending: reasons are current blockers, no effective resolution is invented;
* VİTEL shape: PRODUCT_NOT_FOUND + human-selected product -> effective product is the
  human selection, the historical NOT_FOUND match and stored reasons are unchanged;
* LOGOSOFT shape: automatic match, ``line_resolutions=[]`` -> effective product is the
  automatic match and no human selection is claimed;
* no writes on read, and a corrupt accepted-evidence row never makes detail unreadable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.routers.workbench import _review_item_response
from app.application.execution.exceptions import ExecutionSourceInvoiceIntegrityError
from app.application.workbench import (
    GetReviewItemUseCase,
    LineResolution,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewItem,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
)
from app.application.workbench.dto import ReviewReasonsRole
from app.application.workbench.evidence import ReviewExecutionEvidence, ReviewSourceInvoiceEvidence
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.review_evidence import (
    EFFECTIVE_STATE_UNAVAILABLE,
    EffectiveLineResolutionKind,
    EffectiveProductSource,
    ReviewEvidenceReader,
)
from app.application.workbench.selected_expense_account_resolution import ResolutionAccountRecord
from app.application.workbench.selected_product_resolution import (
    HUMAN_SELECTED_MATCHED_BY,
    ResolutionProductRecord,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
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
REVIEW_ID = "review:vitel"
ETTN = "ettn-19f"
TAX_ID = 401
VITEL_PRODUCT_ID = 392
VITEL_SKU = "85710.1S1"
LOGOSOFT_BASIC_PRODUCT_ID = 393
LOGOSOFT_SKU = "CFQ7TTC0LH18:0001"
EXPENSE_ACCOUNT_ID = 777

PRODUCT_NOT_FOUND_REASON = ManualReviewReason(
    code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
    message="No Odoo product found for line 1.",
    line_number="1",
)


# --------------------------------------------------------------------------- builders


def _invoice(seller_item_code: str) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-19F",
            invoice_uuid=ETTN,
            ettn=ETTN,
            issue_date=date(2026, 9, 1),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1111111111"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("120.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Licence",
                seller_item_code=seller_item_code,
                quantity=Decimal("1"),
                unit_code="NIU",
                unit_price=Decimal("100.00"),
                line_extension_amount=Decimal("100.00"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20"), tax_amount=Decimal("20.00")),),
            ),
        ),
    )


def _product_match(*, product_id: int | None, seller_item_code: str) -> InvoiceProductMatchResult:
    matched = product_id is not None
    return InvoiceProductMatchResult(
        line_results=(
            InvoiceProductLineResult(
                line_number="1",
                result=ProductMatchResult(
                    status=ProductMatchStatus.MATCHED if matched else ProductMatchStatus.NOT_FOUND,
                    line_number="1",
                    product_id=product_id,
                    default_code=None,
                    barcode=None,
                    seller_item_code=seller_item_code,
                    matched_by="default_code" if matched else None,
                    reason="matched" if matched else "No Odoo product found.",
                    candidate_count=1 if matched else 0,
                    confidence=Decimal("1.00") if matched else None,
                ),
            ),
        )
    )


def _tax_match(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
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
            ),
        )
    )


def _stage_one(*, product_id: int | None, seller_item_code: str) -> ReviewExecutionEvidence:
    invoice = _invoice(seller_item_code)
    return ReviewExecutionEvidence(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=101,
            matched_by="tax_number",
            reason="matched",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=_product_match(product_id=product_id, seller_item_code=seller_item_code),
        tax_match=_tax_match(invoice),
    )


def _review_item(reasons: tuple[ManualReviewReason, ...]) -> ReviewItem:
    return ReviewItem(
        review_id=REVIEW_ID,
        invoice_id=ETTN,
        invoice_number="INV-19F",
        supplier_tax_number="1111111111",
        supplier_name="Supplier",
        invoice_date=date(2026, 9, 1),
        currency="TRY",
        total_amount=Decimal("120.00"),
        workflow=WorkflowType.VENDOR_BILL,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=reasons,
    )


def _command(
    line_resolutions: tuple[LineResolution, ...] = (),
    *,
    decision: ReviewDecisionType = ReviewDecisionType.SELECT_WORKFLOW,
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=1,
        decision=decision,
        selected_workflow=WorkflowType.VENDOR_BILL if decision is ReviewDecisionType.SELECT_WORKFLOW else None,
        line_resolutions=line_resolutions,
        decided_by="finance.user",
        idempotency_key="decision:19f",
    )


# --------------------------------------------------------------------------- fixtures / fakes


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


class _SourceReader:
    def __init__(self, invoice: InternalInvoice) -> None:
        self._invoice = invoice

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        return ReviewSourceInvoiceEvidence(
            review_id=review_id,
            company_id=company_id,
            review_version=1,
            source_invoice_id=ETTN,
            invoice=self._invoice,
        )


class _NoPartners:
    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None):
        return ()


class _Products:
    def find_products_by_ids(self, product_ids: tuple[int, ...]) -> tuple[ResolutionProductRecord, ...]:
        return tuple(
            ResolutionProductRecord(
                id=product_id, name="ManageEngine", default_code=VITEL_SKU, barcode=None, active=True, company_id=None
            )
            for product_id in product_ids
        )


class _Accounts:
    def find_accounts_by_ids(self, account_ids: tuple[int, ...]) -> tuple[ResolutionAccountRecord, ...]:
        return tuple(ResolutionAccountRecord(id=account_id, company_ids=(COMPANY_ID,)) for account_id in account_ids)


class _CorruptAcceptedSource:
    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int):
        raise ExecutionSourceInvoiceIntegrityError("Execution source invoice evidence is invalid.")


def _seed(session: Session, *, product_id: int | None, seller_item_code: str, reasons=()) -> None:
    SqlAlchemyReviewRepository(session).create_review_item_with_execution_evidence(
        _review_item(reasons),
        company_id=COMPANY_ID,
        idempotency_key="review-key-19f",
        evidence=_stage_one(product_id=product_id, seller_item_code=seller_item_code),
    )


def _accept(session: Session, command: ReviewDecisionCommand) -> None:
    repository = SqlAlchemyReviewRepository(session)
    acknowledgement = SubmitReviewDecisionUseCase(
        review_decision_writer=repository,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_Products(),
        selected_account_reader=_Accounts(),
    ).execute(command)
    assert acknowledgement.accepted is True


def _detail(session: Session, *, seller_item_code: str, accepted_source_reader=None) -> ReviewItem:
    repository = SqlAlchemyReviewRepository(session)
    return GetReviewItemUseCase(
        review_queue_reader=repository,
        evidence_reader=ReviewEvidenceReader(
            source_reader=_SourceReader(_invoice(seller_item_code)),
            execution_reader=repository,
            partner_repository=_NoPartners(),
            accepted_decision_reader=repository,
            accepted_source_reader=accepted_source_reader or SqlAlchemyExecutionSourceInvoiceReader(session),
        ),
    ).execute(ReviewDetailQuery(review_id=REVIEW_ID, company_id=COMPANY_ID))


def _row_counts(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (
            WorkbenchReviewItem,
            WorkbenchReviewExecutionEvidence,
            WorkbenchReviewDecision,
            ExecutionSourceInvoiceEvidence,
        )
    )


# --------------------------------------------------------------------------- pending review


def test_pending_review_reasons_are_current_blockers_and_no_effective_state_is_invented(session: Session) -> None:
    _seed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))

    item = _detail(session, seller_item_code=VITEL_SKU)

    assert item.status is ReviewStatus.PENDING_REVIEW
    assert item.review_reasons == (PRODUCT_NOT_FOUND_REASON,)
    line = item.evidence.source_lines[0]
    assert line.product_match.status is ProductMatchStatus.NOT_FOUND
    assert line.effective_resolution is None
    assert item.evidence.accepted_decision is None
    assert item.evidence.product_match_review_version == 1
    payload = _review_item_response(item).model_dump()
    assert payload["review_reasons_role"] == ReviewReasonsRole.CURRENT_BLOCKERS.value
    assert payload["evidence"]["source_lines"][0]["effective_resolution"] is None


# --------------------------------------------------------------------------- VİTEL: human-selected


def test_vitel_shape_accepted_human_selection_is_the_effective_product(session: Session) -> None:
    _seed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    stored_reasons_before = session.scalar(select(WorkbenchReviewItem.review_reasons))
    _accept(session, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))
    counts_before_read = _row_counts(session)

    item = _detail(session, seller_item_code=VITEL_SKU)

    assert item.status is ReviewStatus.DECISION_SUBMITTED
    assert item.version == 2
    # Historical reasons are returned verbatim -- never rewritten -- with the decided role.
    assert item.review_reasons == (PRODUCT_NOT_FOUND_REASON,)
    assert session.scalar(select(WorkbenchReviewItem.review_reasons)) == stored_reasons_before
    line = item.evidence.source_lines[0]
    # The matcher's original pre-decision result stays visible (read from version 1).
    assert item.evidence.product_match_review_version == 1
    assert line.product_match.status is ProductMatchStatus.NOT_FOUND
    assert line.product_match.product_id is None
    # ...and the accepted decision's pinned product governs execution.
    effective = line.effective_resolution
    assert effective.kind is EffectiveLineResolutionKind.PRODUCT
    assert effective.product_id == VITEL_PRODUCT_ID
    assert effective.product_source is EffectiveProductSource.HUMAN_SELECTED
    assert effective.matched_by == HUMAN_SELECTED_MATCHED_BY
    assert item.evidence.accepted_decision.decision_version == 2
    assert item.evidence.accepted_decision.decision_type is ReviewDecisionType.SELECT_WORKFLOW
    assert item.evidence.accepted_decision.selected_workflow is WorkflowType.VENDOR_BILL
    assert item.evidence.effective_state_error is None
    # Read-only: the detail read added or changed nothing.
    assert _row_counts(session) == counts_before_read
    assert not session.new and not session.dirty

    payload = _review_item_response(item).model_dump()
    assert payload["review_reasons_role"] == ReviewReasonsRole.DECISION_BASIS.value
    assert payload["review_reasons"][0]["code"] == "PRODUCT_NOT_FOUND"
    source_line = payload["evidence"]["source_lines"][0]
    assert source_line["product_match"]["status"] == ProductMatchStatus.NOT_FOUND.value
    assert source_line["effective_resolution"] == {
        "kind": "product",
        "product_id": VITEL_PRODUCT_ID,
        "product_source": "human_selected",
        "matched_by": HUMAN_SELECTED_MATCHED_BY,
        "match_status": ProductMatchStatus.MATCHED.value,
        "expense_account_id": None,
    }
    assert payload["evidence"]["accepted_decision"]["decision_type"] == "select_workflow"


# --------------------------------------------------------------------------- LOGOSOFT: automatic


def test_logosoft_shape_accepted_automatic_match_claims_no_human_selection(session: Session) -> None:
    _seed(session, product_id=LOGOSOFT_BASIC_PRODUCT_ID, seller_item_code=LOGOSOFT_SKU)
    _accept(session, _command(()))

    item = _detail(session, seller_item_code=LOGOSOFT_SKU)

    line = item.evidence.source_lines[0]
    # Regression: this used to be None after acceptance (Stage-1 read at version 2).
    assert line.product_match is not None
    assert line.product_match.product_id == LOGOSOFT_BASIC_PRODUCT_ID
    effective = line.effective_resolution
    assert effective.kind is EffectiveLineResolutionKind.PRODUCT
    assert effective.product_id == LOGOSOFT_BASIC_PRODUCT_ID
    assert effective.product_source is EffectiveProductSource.AUTOMATIC
    assert effective.matched_by == "default_code"
    assert item.review_reasons == ()


# --------------------------------------------------------------------------- account-only / dismiss


def test_account_only_line_resolution_is_exposed_without_a_product(session: Session) -> None:
    _seed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    _accept(
        session,
        _command((LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID),)),
    )

    effective = _detail(session, seller_item_code=VITEL_SKU).evidence.source_lines[0].effective_resolution

    assert effective.kind is EffectiveLineResolutionKind.ACCOUNT_ONLY
    assert effective.product_id is None
    assert effective.product_source is None
    assert effective.expense_account_id == EXPENSE_ACCOUNT_ID


def test_dismissed_review_has_decision_but_no_effective_resolution(session: Session) -> None:
    _seed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    _accept(session, _command(decision=ReviewDecisionType.DISMISS))

    item = _detail(session, seller_item_code=VITEL_SKU)

    assert item.status is ReviewStatus.DISMISSED
    assert item.evidence.accepted_decision.decision_type is ReviewDecisionType.DISMISS
    assert item.evidence.source_lines[0].effective_resolution is None
    assert item.evidence.source_lines[0].product_match.status is ProductMatchStatus.NOT_FOUND
    assert _review_item_response(item).review_reasons_role == ReviewReasonsRole.DECISION_BASIS.value


# --------------------------------------------------------------------------- degradation / compatibility


def test_corrupt_accepted_evidence_keeps_detail_readable_and_reports_it(session: Session) -> None:
    _seed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    _accept(session, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))

    item = _detail(session, seller_item_code=VITEL_SKU, accepted_source_reader=_CorruptAcceptedSource())

    assert item.evidence.effective_state_error == EFFECTIVE_STATE_UNAVAILABLE
    assert item.evidence.accepted_decision is None
    assert item.evidence.source_lines[0].effective_resolution is None
    assert item.review_reasons == (PRODUCT_NOT_FOUND_REASON,)


def test_reader_without_decision_readers_keeps_previous_contract(session: Session) -> None:
    _seed(session, product_id=LOGOSOFT_BASIC_PRODUCT_ID, seller_item_code=LOGOSOFT_SKU)
    repository = SqlAlchemyReviewRepository(session)

    evidence = ReviewEvidenceReader(
        source_reader=_SourceReader(_invoice(LOGOSOFT_SKU)),
        execution_reader=repository,
        partner_repository=_NoPartners(),
    ).get(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1)

    assert evidence.source_lines[0].product_match.product_id == LOGOSOFT_BASIC_PRODUCT_ID
    assert evidence.source_lines[0].effective_resolution is None
    assert evidence.accepted_decision is None


def test_production_wiring_supplies_the_decision_aware_readers(session: Session) -> None:
    from app.api.dependencies import get_review_evidence_reader

    reader = get_review_evidence_reader(session, object())

    assert isinstance(reader._accepted_decision_reader, SqlAlchemyReviewRepository)
    assert isinstance(reader._accepted_source_reader, SqlAlchemyExecutionSourceInvoiceReader)
