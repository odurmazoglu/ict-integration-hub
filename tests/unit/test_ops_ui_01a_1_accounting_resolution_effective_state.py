"""OPS-UI-01A-1: accepted invoice-level accounting resolution in the effective state.

A review-scoped accounting resolution (P0-PROD-15T) -- like a supplier-wide
operating-expense mapping -- is pinned at invoice level in
``ExecutionSourceInvoice.operating_expense_match``. When it applies, the Vendor Bill
builder books *every* line to that one account. PR #197's ``effective_resolutions``
ignored it and reported such lines as ``unresolved`` (production: CloudSpark,
account.move 63, account 247). These tests pin the effective state to exactly what
execution uses, for both the review-detail API and the Odoo Workbench projection.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.routers.workbench import _review_item_response
from app.application.execution.contracts import AcceptedReviewDecision, ExecutionSourceInvoice
from app.application.execution.exceptions import ExecutionPlanningError
from app.application.expense_mapping import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.application.expense_mapping.exceptions import OperatingExpenseMappingContractError
from app.application.use_cases.effective_decision import (
    REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY as CANONICAL_ACCOUNTING_RESOLUTION_MATCHED_BY,
)
from app.application.workbench import LineResolution, ReviewItem, ReviewStatus
from app.application.workbench.projection_sync import WorkbenchProjectionSources, WorkbenchProjectionSynchronizer
from app.application.workbench.review_evidence import (
    EFFECTIVE_STATE_UNAVAILABLE,
    REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY,
    EffectiveLineResolutionKind,
    effective_resolutions,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.billing import VendorBillBuilder
from app.billing.builder import _operating_expense_mode
from app.db.base import Base
from app.erp.odoo.workbench_projection_publisher import OdooWorkbenchProjectionPublisher
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from tests.unit.test_operating_expense_vendor_bill_builder import (
    _identifier_free_products,
    _invoice,
    _line,
    _partner,
    _taxes,
)
from tests.unit.test_ops_ui_01a_workbench_projection_sync import StudioFake, _mapping
from tests.unit.test_p0_prod_19f_review_lifecycle_effective_state import (
    VITEL_PRODUCT_ID,
    VITEL_SKU,
    _accept,
    _command,
    _detail,
    _seed,
)

COMPANY_ID = 1
REVIEW_ID = "review:accounting-resolution"
ACCOUNT_ID = 247
PARTNER_ID = 101  # _partner() from the builder helpers


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
    with sessionmaker(bind=engine)() as db_session:
        yield db_session


def _expense_match(*, matched_by: str = REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY) -> OperatingExpenseMatchResult:
    return OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="Resolved via an accepted review-scoped accounting resolution for this review.",
        candidate_count=1,
        mapping_id=1,
        company_id=COMPANY_ID,
        vendor_partner_id=PARTNER_ID,
        expense_account_id=ACCOUNT_ID,
        expense_category="general_expense",
        matched_by=matched_by,
        confidence=Decimal("1.00"),
    )


def _cloudspark_source(
    *,
    matched_by: str = REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY,
    line_resolutions: tuple[LineResolution, ...] = (),
    operating_expense_match: OperatingExpenseMatchResult | None | object = ...,
) -> ExecutionSourceInvoice:
    """CloudSpark shape: three identifier-free lines, no per-line decisions, one accepted invoice account."""

    invoice = _invoice([_line("1"), _line("2"), _line("3")])
    return ExecutionSourceInvoice(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=4,
        source_invoice_id="ettn-cloudspark-shape",
        invoice=invoice,
        partner_match=_partner(),
        product_match=_identifier_free_products(invoice),
        tax_match=_taxes(invoice),
        operating_expense_match=(
            _expense_match(matched_by=matched_by) if operating_expense_match is ... else operating_expense_match
        ),
        line_resolutions=line_resolutions,
    )


def _built_accounts(source: ExecutionSourceInvoice) -> dict[str, int]:
    """What execution actually books: the Vendor Bill builder's per-line account."""

    account_only = frozenset(r.line_number for r in source.line_resolutions if r.account_only)
    explicit = {r.line_number: r.expense_account_id for r in source.line_resolutions if r.account_only}
    bill = VendorBillBuilder().build(
        source.invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        company_id=COMPANY_ID,
        operating_expense_match=source.operating_expense_match,
        account_only_line_numbers=account_only,
        account_only_expense_match=source.account_only_expense_match,
        explicit_account_only_accounts=explicit,
    )
    # Bill lines are built in invoice-line order.
    return {
        line.line_number: bill_line.account_id
        for line, bill_line in zip(source.invoice.lines, bill.invoice_lines, strict=True)
    }


# --------------------------------------------------------------------------- 1. CloudSpark-equivalent


def test_accepted_accounting_resolution_resolves_every_line_to_the_accepted_account() -> None:
    source = _cloudspark_source()

    resolutions = effective_resolutions(source)

    assert set(resolutions) == {"1", "2", "3"}
    for resolution in resolutions.values():
        assert resolution.kind is EffectiveLineResolutionKind.ACCOUNTING_RESOLUTION
        assert resolution.expense_account_id == ACCOUNT_ID
        assert resolution.product_id is None and resolution.product_source is None
        assert resolution.matched_by == REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY
        assert resolution.match_status == "MATCHED"
    # Effective state is exactly what execution books.
    assert {line: r.expense_account_id for line, r in resolutions.items()} == _built_accounts(source)


def test_supplier_wide_operating_expense_mapping_is_reported_as_its_own_source() -> None:
    source = _cloudspark_source(matched_by="company_partner")

    resolutions = effective_resolutions(source)

    assert {r.kind for r in resolutions.values()} == {EffectiveLineResolutionKind.OPERATING_EXPENSE_MAPPING}
    assert {r.expense_account_id for r in resolutions.values()} == {ACCOUNT_ID}
    assert {line: r.expense_account_id for line, r in resolutions.items()} == _built_accounts(source)


def test_matched_by_constant_is_the_canonical_accounting_resolution_marker() -> None:
    assert REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY == CANONICAL_ACCOUNTING_RESOLUTION_MATCHED_BY


# --------------------------------------------------------------------------- 6. precedence


def test_invoice_level_resolution_wins_over_per_line_account_only_exactly_as_execution_does() -> None:
    """The builder ignores per-line account-only decisions in whole-invoice expense mode."""

    source = _cloudspark_source(
        line_resolutions=(LineResolution(line_number="2", account_only=True, expense_account_id=999),)
    )

    resolutions = effective_resolutions(source)

    assert _built_accounts(source) == {"1": ACCOUNT_ID, "2": ACCOUNT_ID, "3": ACCOUNT_ID}
    assert {line: r.expense_account_id for line, r in resolutions.items()} == _built_accounts(source)
    assert resolutions["2"].kind is EffectiveLineResolutionKind.ACCOUNTING_RESOLUTION


@pytest.mark.parametrize(
    ("match", "identifier_free"),
    [
        (_expense_match(), True),
        (_expense_match(), False),
        (_expense_match(matched_by="company_partner"), True),
        (None, True),
        (
            OperatingExpenseMatchResult(status=OperatingExpenseMatchStatus.NOT_FOUND, reason="none"),
            True,
        ),
    ],
)
def test_invoice_level_predicate_is_the_builders_operating_expense_mode(match, identifier_free: bool) -> None:
    from app.application.workbench.review_evidence import _invoice_level_expense_account

    invoice = _invoice([_line("1")] if identifier_free else [_line("1", seller_item_code="SKU-9")])
    source = SimpleNamespace(operating_expense_match=match, invoice=invoice)

    assert (_invoice_level_expense_account(source) is not None) is _operating_expense_mode(invoice, match)


def test_product_backed_lines_cannot_coexist_with_invoice_level_resolution() -> None:
    """A product-resolved line would make the pinned evidence invalid -- no overwrite is possible."""

    source = _cloudspark_source()
    first = source.product_match.line_results[0]
    matched_first = replace(first, result=replace(first.result, status=first.result.status.MATCHED, product_id=5))

    with pytest.raises(ExecutionPlanningError):
        replace(
            source,
            product_match=replace(
                source.product_match, line_results=(matched_first, *source.product_match.line_results[1:])
            ),
        )


# --------------------------------------------------------------------------- 8. historical compatibility


def test_evidence_without_operating_expense_match_keeps_the_previous_per_line_semantics() -> None:
    source = _cloudspark_source(
        operating_expense_match=None,
        line_resolutions=(LineResolution(line_number="1", account_only=True, expense_account_id=ACCOUNT_ID),),
    )

    resolutions = effective_resolutions(source)

    assert resolutions["1"].kind is EffectiveLineResolutionKind.ACCOUNT_ONLY
    assert resolutions["1"].expense_account_id == ACCOUNT_ID
    assert resolutions["2"].kind is EffectiveLineResolutionKind.UNRESOLVED
    assert resolutions["3"].kind is EffectiveLineResolutionKind.UNRESOLVED


# --------------------------------------------------------------------------- 7. malformed evidence


@pytest.mark.parametrize(
    "error",
    [
        OperatingExpenseMappingContractError(
            "A MATCHED operating-expense result must expose mapping, account, and category."
        ),
        ExecutionPlanningError(
            "operating-expense evidence requires an invoice free of deterministic product identifiers."
        ),
    ],
)
def test_malformed_accounting_resolution_evidence_is_an_effective_state_error_in_review_detail(
    session, error: Exception
) -> None:
    class CorruptSource:
        def get_source_invoice(self, **kwargs):
            raise error

    _seed(session, product_id=None, seller_item_code=VITEL_SKU)
    _accept(session, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))

    item = _detail(session, seller_item_code=VITEL_SKU, accepted_source_reader=CorruptSource())

    assert item.evidence.effective_state_error == EFFECTIVE_STATE_UNAVAILABLE
    assert item.evidence.source_lines[0].effective_resolution is None


# --------------------------------------------------------------------------- 10. review-detail API


def test_review_detail_api_exposes_the_accounting_resolution_effective_state(session) -> None:
    class AcceptedSource:
        def get_source_invoice(self, **kwargs):
            return _cloudspark_source()

    _seed(session, product_id=None, seller_item_code=VITEL_SKU)
    _accept(session, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))

    item = _detail(session, seller_item_code=VITEL_SKU, accepted_source_reader=AcceptedSource())
    payload = _review_item_response(item).model_dump()

    assert payload["evidence"]["effective_state_error"] is None
    assert payload["evidence"]["source_lines"][0]["effective_resolution"] == {
        "kind": "accounting_resolution",
        "product_id": None,
        "product_source": None,
        "matched_by": REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY,
        "match_status": "MATCHED",
        "expense_account_id": ACCOUNT_ID,
        "asset_account_id": None,
        "depreciation_model_id": None,
    }


# --------------------------------------------------------------------------- 9. Workbench projection


def _projection_synchronizer(source_reader, studio: StudioFake) -> WorkbenchProjectionSynchronizer:
    review = ReviewItem(
        review_id=REVIEW_ID,
        invoice_id="ettn-cloudspark-shape",
        invoice_number="I082026000000009",
        supplier_tax_number="1760390647",
        supplier_name="Supplier",
        invoice_date=None,
        currency="TRY",
        total_amount=Decimal("5951.76"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.DECISION_SUBMITTED,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
                message="Operating expense mapping is required.",
            ),
        ),
        version=4,
    )

    class Reviews:
        def get_review_item(self, query):
            return review

    class Decisions:
        def get_accepted_decision(self, *, review_id, company_id, decision_version):
            return AcceptedReviewDecision(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                decision_id="decision:accounting-resolution",
                selected_workflow=WorkflowType.VENDOR_BILL,
            )

    class NoExecution:
        def find_latest_snapshot_for_review(self, **kwargs):
            return None

    @contextmanager
    def read_scope():
        yield WorkbenchProjectionSources(
            review_reader=Reviews(),
            accepted_decision_reader=Decisions(),
            accepted_source_reader=source_reader,
            execution_snapshot_reader=NoExecution(),
            publisher=OdooWorkbenchProjectionPublisher(adapter=studio, mapping=_mapping()),
        )

    return WorkbenchProjectionSynchronizer(read_scope=read_scope)


def test_workbench_projection_renders_accounting_resolution_lines() -> None:
    class AcceptedSource:
        def get_source_invoice(self, **kwargs):
            return _cloudspark_source()

    studio = StudioFake()

    result = _projection_synchronizer(AcceptedSource(), studio).plan(review_id=REVIEW_ID, company_id=COMPANY_ID)

    reasons = next(change.after for change in result.changes if change.field == "x_studio_review_reasons")
    for line in ("1", "2", "3"):
        assert f"Line {line} → account {ACCOUNT_ID} (accounting resolution)" in reasons
    assert "unresolved" not in reasons
    assert "account only" not in reasons
    assert studio.write_count == 0


def test_workbench_projection_reports_malformed_accounting_evidence_as_effective_state_error() -> None:
    class CorruptSource:
        def get_source_invoice(self, **kwargs):
            raise OperatingExpenseMappingContractError("A MATCHED operating-expense result must expose an account.")

    studio = StudioFake()

    result = _projection_synchronizer(CorruptSource(), studio).plan(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome.value == "created"
    reasons = next(change.after for change in result.changes if change.field == "x_studio_review_reasons")
    assert EFFECTIVE_STATE_UNAVAILABLE in reasons
    assert "→" not in reasons
