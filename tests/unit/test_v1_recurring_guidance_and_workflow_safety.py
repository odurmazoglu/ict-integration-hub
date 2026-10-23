"""V1: Direct Vendor Bill-only decisions and previous-invoice guidance for recurring suppliers."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.application.workbench.accounting_resolution import AccountingTreatmentType, ReviewAccountingResolution
from app.application.workbench.allocations import (
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
)
from app.application.workbench.dto import ReviewDecisionType, ReviewItem, ReviewStatus
from app.application.workbench.exceptions import (
    OperatorDecisionCandidateInvalidError,
    OperatorDecisionWorkflowNotSupportedError,
    ReviewVersionConflictError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateReadError,
    WorkbenchCandidateWorkflowError,
)
from app.application.workbench.operator_guidance import (
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
)
from app.application.workbench.operator_request_handlers import (
    V1_UNSUPPORTED_WORKFLOW_MESSAGE,
    DecisionRequestHandler,
)
from app.application.workbench.operator_request_ingestion import OperatorRequestAction, OperatorRequestOutcome
from app.application.workbench.previous_invoice_guidance import (
    PreviousInvoiceDecision,
    build_previous_invoice_guidance,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.composition.imports import _operator_guidance_facts
from app.core.config import get_settings
from app.db.base import Base
from app.erp.odoo.workbench_candidate_reader import _selected_workflow
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.models.workflow_execution import WorkflowExecution
from app.persistence.previous_invoice_decision_reader import SqlAlchemyPreviousInvoiceDecisionReader
from tests.unit.test_adr_0013_operator_request_ingestion import (
    FakeAcknowledger,
    FakeDecisionReader,
    FakeLedger,
    FakeReader,
    FakeSubmitter,
    FakeUnitOfWork,
    FakeValidator,
    _candidate,
    _context,
    _decision_request,
    _workflow,
)
from tests.unit.test_postgres_migration_compatibility import _alembic, _create_database, _drop_database

CLOUDSPARK_VKN = "1760390647"
OTHER_VKN = "1111111111"
COMPANY = 1
DECIDED_AT = datetime(2026, 10, 22, 8, 10, tzinfo=UTC)


# =========================================================================== workflow safety


class RaisingDecisionReader:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_ready_decision(self, *, review_id: str, company_id: int):
        raise self.error


def _handler(reader: Any, submitter: FakeSubmitter | None = None) -> DecisionRequestHandler:
    return DecisionRequestHandler(
        candidate_reader=reader,
        erp_reference_validator=FakeValidator(),
        decision_submitter=submitter or FakeSubmitter(),
        unit_of_work=FakeUnitOfWork(),
    )


def _allocations(allocation_type: BusinessContextAllocationType) -> BusinessContextAllocationSet:
    return BusinessContextAllocationSet(
        allocations=(
            BusinessContextAllocation(
                allocation_key="a1",
                allocation_type=allocation_type,
                amount=Decimal("10.00"),
                currency="TRY",
                purchase_order_id=7,
            ),
        ),
        invoice_total=Decimal("10.00"),
        currency="TRY",
    )


@pytest.mark.parametrize(
    "workflow", [WorkflowType.EXPENSE, WorkflowType.ASSET, WorkflowType.RFQ, WorkflowType.SUBSCRIPTION]
)
def test_unsupported_workflow_is_rejected_without_submitting(workflow: WorkflowType) -> None:
    submitter = FakeSubmitter()
    with pytest.raises(OperatorDecisionWorkflowNotSupportedError) as raised:
        _handler(FakeDecisionReader(_candidate(selected_workflow=workflow)), submitter).handle(
            _decision_request(), _context()
        )
    assert raised.value.safe_message == V1_UNSUPPORTED_WORKFLOW_MESSAGE
    assert submitter.commands == []


def test_existing_purchase_order_maps_to_vendor_bill_but_is_not_direct_vendor_bill() -> None:
    candidate = _candidate(
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=_allocations(BusinessContextAllocationType.EXISTING_PURCHASE_ORDER),
    )
    submitter = FakeSubmitter()
    with pytest.raises(OperatorDecisionWorkflowNotSupportedError):
        _handler(FakeDecisionReader(candidate), submitter).handle(_decision_request(), _context())
    assert submitter.commands == []


def test_direct_vendor_bill_and_dismiss_are_accepted() -> None:
    for candidate in (
        _candidate(selected_workflow=WorkflowType.VENDOR_BILL),
        _candidate(decision=ReviewDecisionType.DISMISS, selected_workflow=None, comment="mükerrer fatura"),
    ):
        submitter = FakeSubmitter()
        outcome = _handler(FakeDecisionReader(candidate), submitter).handle(_decision_request(), _context())
        assert outcome.outcome is OperatorRequestOutcome.COMPLETED
        assert len(submitter.commands) == 1


def test_an_already_accepted_decision_is_replayed_unchanged() -> None:
    # Preserve existing accepted decisions: the V1 gate applies only to new decisions.
    candidate = _candidate(selected_workflow=WorkflowType.EXPENSE)
    outcome = _handler(FakeDecisionReader(candidate), FakeSubmitter(matching=True)).handle(
        _decision_request(), _context()
    )
    assert outcome.outcome is OperatorRequestOutcome.ALREADY_COMPLETED


def test_stale_version_is_still_reported_as_stale() -> None:
    with pytest.raises(ReviewVersionConflictError):
        _handler(FakeDecisionReader(_candidate(expected_version=5, selected_workflow=WorkflowType.EXPENSE))).handle(
            _decision_request(), _context()
        )


def test_candidate_data_errors_become_deterministic_rejections() -> None:
    with pytest.raises(OperatorDecisionWorkflowNotSupportedError):
        _handler(RaisingDecisionReader(WorkbenchCandidateWorkflowError("x"))).handle(_decision_request(), _context())
    with pytest.raises(OperatorDecisionCandidateInvalidError):
        _handler(RaisingDecisionReader(WorkbenchCandidateDataError("x"))).handle(_decision_request(), _context())
    # Unreachable Odoo stays retryable.
    with pytest.raises(WorkbenchCandidateReadError):
        _handler(RaisingDecisionReader(WorkbenchCandidateReadError("x"))).handle(_decision_request(), _context())


def test_reader_types_every_workflow_problem_as_a_workflow_error() -> None:
    with pytest.raises(WorkbenchCandidateWorkflowError):
        _selected_workflow("Operating Expense", None)  # requires an operating_expense allocation
    with pytest.raises(WorkbenchCandidateWorkflowError):
        _selected_workflow("Bilinmeyen", None)
    with pytest.raises(WorkbenchCandidateWorkflowError):
        _selected_workflow("Direct Vendor Bill", _allocations(BusinessContextAllocationType.NEW_RFQ_PURCHASE))
    assert _selected_workflow("Direct Vendor Bill", None) is WorkflowType.VENDOR_BILL
    assert issubclass(WorkbenchCandidateWorkflowError, WorkbenchCandidateDataError)


def test_cb0604aa_case_is_rejected_once_with_a_turkish_message_and_never_retried() -> None:
    """The 2026-10-22 UAT failure: "Operating Expense" without allocation was retried 5x."""

    ledger, acknowledger = FakeLedger(), FakeAcknowledger()
    handler = _handler(RaisingDecisionReader(WorkbenchCandidateWorkflowError("invalid")))
    workflow = _workflow(
        FakeReader([_decision_request()]),
        handler,
        ledger=ledger,
        acknowledger=acknowledger,
        action=OperatorRequestAction.DECISION,
    )
    result = workflow.run(company_id=COMPANY).results[0]
    assert result.outcome is OperatorRequestOutcome.REJECTED
    assert "yalnız 'Direct Vendor Bill'" in result.message
    assert all(entry.attempts == 0 for entry in ledger.rows.values())
    assert len(acknowledger.calls) == 1


# =========================================================================== previous-invoice selection


def _resolution(review_id: str, version: int = 2, **overrides: Any) -> ReviewAccountingResolution:
    values: dict[str, Any] = {
        "id": 4,
        "review_id": review_id,
        "company_id": COMPANY,
        "review_version": version,
        "treatment_type": AccountingTreatmentType.EXPENSE_ACCOUNT,
        "expense_account_id": 247,
        "expense_category": "CLOUD_SERVICES_INTERNAL",
    }
    values.update(overrides)
    return ReviewAccountingResolution(**values)


def _previous(review_id: str, invoice_date: date, **overrides: Any) -> PreviousInvoiceDecision:
    resolution_overrides = overrides.pop("resolution", {})
    values: dict[str, Any] = {
        "review_id": review_id,
        "invoice_number": f"INV-{review_id}",
        "invoice_date": invoice_date,
        "supplier_name": "CLOUDSPARK BULUT TEKNOLOJİLERİ",
        "decided_at": DECIDED_AT,
        "purchase_purpose": PurchasePurpose.INTERNAL_USE,
        "accounting_resolution": _resolution(review_id, **resolution_overrides),
    }
    values.update(overrides)
    return PreviousInvoiceDecision(**values)


def test_no_completed_previous_invoice_means_no_guidance() -> None:
    assert build_previous_invoice_guidance((), supplier_tax_number=CLOUDSPARK_VKN) is None


def test_most_recent_invoice_is_shown_among_several() -> None:
    older = _previous("a", date(2026, 9, 8), resolution={"expense_category": "IT_HARDWARE_INTERNAL"})
    newer = _previous("b", date(2026, 9, 28))
    guidance = build_previous_invoice_guidance(
        (older, newer), supplier_tax_number=CLOUDSPARK_VKN, resolve_labels=lambda _r: ("770000 — Genel Giderler", None)
    )
    assert guidance is not None
    assert guidance.latest.review_id == "b"
    assert guidance.account_label == "770000 — Genel Giderler"
    assert guidance.completed_count == 2


def test_same_invoice_date_falls_back_to_the_later_decision() -> None:
    first = _previous("a", date(2026, 9, 28))
    second = _previous("b", date(2026, 9, 28), decided_at=DECIDED_AT + timedelta(hours=1))
    guidance = build_previous_invoice_guidance((second, first), supplier_tax_number=OTHER_VKN)
    assert guidance is not None and guidance.latest.review_id == "b"


def test_mixed_purpose_supplier_is_flagged() -> None:
    hardware = _previous("a", date(2026, 9, 8), resolution={"expense_category": "IT_HARDWARE_INTERNAL"})
    cloud = _previous("b", date(2026, 9, 28))
    resale = _previous("c", date(2026, 8, 1), purchase_purpose=PurchasePurpose.RESALE)
    guidance = build_previous_invoice_guidance((hardware, cloud, resale), supplier_tax_number=OTHER_VKN)
    assert guidance is not None and guidance.mixed_purpose
    assert guidance.distinct_classification_count == 3

    uniform = build_previous_invoice_guidance((_previous("a", date(2026, 9, 8)), cloud), supplier_tax_number=OTHER_VKN)
    assert uniform is not None and not uniform.mixed_purpose


def test_cloudspark_carries_the_legacy_accounting_confirmation_note() -> None:
    guidance = build_previous_invoice_guidance((_previous("b", date(2026, 9, 28)),), supplier_tax_number=CLOUDSPARK_VKN)
    assert guidance is not None and "740.30.03" in (guidance.supplier_note or "")
    other = build_previous_invoice_guidance((_previous("b", date(2026, 9, 28)),), supplier_tax_number=OTHER_VKN)
    assert other is not None and other.supplier_note is None


# =========================================================================== guidance rendering


def _source(status: ReviewStatus = ReviewStatus.PENDING_REVIEW, codes=None) -> GuidanceInput:
    return GuidanceInput(
        status=status,
        reason_codes=tuple(
            codes if codes is not None else (ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,)
        ),
        supplier_name="CLOUDSPARK BULUT TEKNOLOJİLERİ",
        decision_type=None,
        decision_workflow=None,
        decision_version=None,
        execution_state=None,
        vendor_bill_id=None,
    )


def _cloudspark_guidance():
    return build_previous_invoice_guidance(
        (
            _previous("a", date(2026, 9, 8), resolution={"expense_category": "IT_HARDWARE_INTERNAL"}),
            _previous("b", date(2026, 9, 28), invoice_number="I282026000000141"),
        ),
        supplier_tax_number=CLOUDSPARK_VKN,
        resolve_labels=lambda _r: ("770000 — Genel Giderler", None),
    )


@pytest.mark.parametrize(
    ("purpose", "expected_action"),
    [(None, OperatorNextAction.PURPOSE), (PurchasePurpose.INTERNAL_USE, OperatorNextAction.ACCOUNTING)],
)
def test_history_is_shown_at_purpose_and_accounting_steps(purpose, expected_action) -> None:
    facts = OperatorGuidanceFacts(current_purchase_purpose=purpose, previous_invoice=_cloudspark_guidance())
    guidance = build_operator_guidance(_source(), facts)
    assert guidance.next_action is expected_action
    todo = guidance.todo_html
    assert "Geçmiş bilgi — öneri değildir" in todo
    assert "CLOUDSPARK BULUT TEKNOLOJİLERİ" in todo
    assert "I282026000000141 (2026-09-28)" in todo
    assert "Şirket İçi Kullanım" in todo
    assert "770000 — Genel Giderler" in todo
    assert "CLOUD_SERVICES_INTERNAL" in todo
    assert "teyit gerektirir" in todo
    assert "karma amaçlı tedarikçi" in todo
    assert "740.30.03" in todo
    assert "onaylanmış bir kural değildir" in todo


def test_history_is_not_shown_elsewhere_and_never_changes_the_step() -> None:
    history = _cloudspark_guidance()
    for source, facts in (
        (_source(codes=(ManualReviewReasonCode.SUPPLIER_NOT_FOUND,)), OperatorGuidanceFacts()),
        (_source(codes=()), OperatorGuidanceFacts()),  # decision step
        (_source(status=ReviewStatus.DECISION_SUBMITTED, codes=()), OperatorGuidanceFacts()),
    ):
        without = build_operator_guidance(source, facts)
        with_history = build_operator_guidance(
            source, OperatorGuidanceFacts(**{**_facts_dict(facts), "previous_invoice": history})
        )
        assert with_history == without
        assert "Geçmiş bilgi" not in with_history.todo_html


def test_history_never_marks_purpose_or_accounting_done() -> None:
    facts = OperatorGuidanceFacts(previous_invoice=_cloudspark_guidance())
    guidance = build_operator_guidance(_source(), facts)
    assert guidance.next_action is OperatorNextAction.PURPOSE
    assert "Satın alma amacı" not in guidance.completed_html
    assert "Muhasebe" not in guidance.completed_html


def test_history_text_is_html_escaped() -> None:
    guidance = build_previous_invoice_guidance(
        (_previous("b", date(2026, 9, 28), supplier_name="<b>X</b>"),), supplier_tax_number=OTHER_VKN
    )
    todo = build_operator_guidance(_source(), OperatorGuidanceFacts(previous_invoice=guidance)).todo_html
    assert "<b>X</b>" not in todo and "&lt;b&gt;X&lt;/b&gt;" in todo


def _facts_dict(facts: OperatorGuidanceFacts) -> dict[str, Any]:
    return {name: getattr(facts, name) for name in OperatorGuidanceFacts.__dataclass_fields__}


# =========================================================================== persistence reader


@pytest.fixture(params=["sqlite", "postgresql"])
def session(request, monkeypatch):
    """SQLite always; PostgreSQL at Alembic head (the production schema) when
    POSTGRES_TEST_DATABASE_URL is set."""

    if request.param == "sqlite":
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            yield db
        return
    base_url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is required for the PostgreSQL reader check.")
    name = f"ict_v1prev_{uuid4().hex}"
    _create_database(base_url, name)
    url = make_url(base_url).set(database=name).render_as_string(hide_password=False)
    try:
        _alembic(monkeypatch, url, "upgrade", "head")
        engine = create_engine(url)
        with Session(engine) as db:
            yield db
        engine.dispose()
    finally:
        get_settings.cache_clear()
        _drop_database(base_url, name)


def _review(db: Session, review_id: str, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "review_id": review_id,
        "company_id": COMPANY,
        "invoice_id": f"inv-{review_id}",
        "invoice_number": f"I-{review_id}",
        "supplier_tax_number": CLOUDSPARK_VKN,
        "supplier_name": "CLOUDSPARK BULUT TEKNOLOJİLERİ",
        "invoice_date": date(2026, 9, 28),
        "workflow": "vendor_bill",
        "status": "decision_submitted",
        "review_reasons": [],
        "warnings": [],
        "version": 4,
        "idempotency_key": f"ik-{review_id}",
    }
    values.update(overrides)
    db.add(WorkbenchReviewItem(**values))
    db.flush()  # dependants reference the review by FK


def _decision(db: Session, review_id: str, *, before: int = 3, workflow: str | None = "vendor_bill", **kw: Any) -> None:
    db.add(
        WorkbenchReviewDecision(
            decision_id=f"d-{review_id}-{before}",
            review_id=review_id,
            company_id=COMPANY,
            review_version_before=before,
            review_version_after=before + 1,
            decision_type=kw.get("decision_type", "select_workflow"),
            selected_workflow=workflow,
            line_resolutions=[],
            tax_resolutions=[],
            decided_by="odoo:2",
            idempotency_key=f"k-{review_id}-{before}",
            submitted_at=kw.get("submitted_at", DECIDED_AT),
        )
    )


def _execution(
    db: Session, review_id: str, *, version: int = 4, state: str = "completed", mode: str = "execute"
) -> None:
    db.add(
        WorkflowExecution(
            execution_id=f"e-{review_id}-{version}-{mode}",
            review_id=review_id,
            decision_version=version,
            company_id=COMPANY,
            state=state,
            mode=mode,
            idempotency_key=f"ek-{review_id}-{version}-{mode}",
            plan_signature="sig",
            plan={},
            checkpoint={},
            retry_policy={},
        )
    )


def _purpose(db: Session, review_id: str, version: int, purpose: str = "internal_use") -> None:
    db.add(
        WorkbenchReviewPurchasePurposeResolution(
            review_id=review_id,
            company_id=COMPANY,
            review_version=version,
            source_invoice_id=f"src-{review_id}",
            purchase_purpose=purpose,
        )
    )


def _accounting(db: Session, review_id: str, version: int, category: str = "CLOUD_SERVICES_INTERNAL") -> None:
    db.add(
        WorkbenchReviewAccountingResolution(
            review_id=review_id,
            company_id=COMPANY,
            review_version=version,
            treatment_type="expense_account",
            expense_account_id=247,
            expense_category=category,
        )
    )


def _completed(db: Session, review_id: str, **review_overrides: Any) -> None:
    _review(db, review_id, **review_overrides)
    _decision(db, review_id)
    _execution(db, review_id)
    _purpose(db, review_id, 2)
    _accounting(db, review_id, 2)


def _list(db: Session, exclude: str = "review:new") -> tuple[PreviousInvoiceDecision, ...]:
    return SqlAlchemyPreviousInvoiceDecisionReader(db).list_completed_supplier_decisions(
        company_id=COMPANY, supplier_tax_number=CLOUDSPARK_VKN, exclude_review_id=exclude
    )


def test_reader_returns_completed_invoices_most_recent_first(session: Session) -> None:
    _completed(session, "review:b9", invoice_date=date(2026, 9, 8))
    _completed(session, "review:cb", invoice_date=date(2026, 9, 28))
    session.commit()
    decisions = _list(session)
    assert [item.review_id for item in decisions] == ["review:cb", "review:b9"]
    latest = decisions[0]
    assert latest.purchase_purpose is PurchasePurpose.INTERNAL_USE
    assert latest.accounting_resolution.expense_account_id == 247
    assert latest.accounting_resolution.expense_category == "CLOUD_SERVICES_INTERNAL"


def test_reader_skips_incomplete_and_foreign_history(session: Session) -> None:
    _completed(session, "review:ok")
    # decision accepted but no completed live execution (dry-run / failed / none)
    _review(session, "review:dry")
    _decision(session, "review:dry")
    _execution(session, "review:dry", mode="dry_run")
    _purpose(session, "review:dry", 2)
    _accounting(session, "review:dry", 2)
    _review(session, "review:failed")
    _decision(session, "review:failed")
    _execution(session, "review:failed", state="failed")
    _purpose(session, "review:failed", 2)
    _accounting(session, "review:failed", 2)
    # completed but missing a recorded purpose
    _review(session, "review:nopurpose")
    _decision(session, "review:nopurpose")
    _execution(session, "review:nopurpose")
    _accounting(session, "review:nopurpose", 2)
    # dismissed
    _review(session, "review:dismissed")
    _decision(session, "review:dismissed", workflow=None, decision_type="dismiss")
    _execution(session, "review:dismissed")
    _purpose(session, "review:dismissed", 2)
    _accounting(session, "review:dismissed", 2)
    # another supplier and another company
    _completed(session, "review:other", supplier_tax_number=OTHER_VKN)
    _completed(session, "review:co2", company_id=2)
    # the review being guided is excluded
    _completed(session, "review:new")
    session.commit()
    assert [item.review_id for item in _list(session)] == ["review:ok"]


def test_reader_uses_resolutions_recorded_up_to_the_decided_version(session: Session) -> None:
    _review(session, "review:x")
    _decision(session, "review:x", before=3)
    _execution(session, "review:x")
    _purpose(session, "review:x", 1, purpose="resale")  # superseded at v2
    _purpose(session, "review:x", 2)
    _accounting(session, "review:x", 2)
    session.commit()
    (decision,) = _list(session)
    assert decision.purchase_purpose is PurchasePurpose.INTERNAL_USE
    assert decision.accounting_resolution.review_version == 2


def test_guidance_facts_never_write_or_create_supplier_mappings(session: Session) -> None:
    _completed(session, "review:cb", invoice_number="I282026000000141")
    session.commit()
    review = ReviewItem(
        review_id="review:new",
        invoice_id="inv-new",
        invoice_number="I-NOV",
        supplier_tax_number=CLOUDSPARK_VKN,
        supplier_name="CLOUDSPARK BULUT TEKNOLOJİLERİ",
        invoice_date=date(2026, 11, 28),
        currency="TRY",
        total_amount=Decimal("100.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        version=1,
    )
    from app.persistence.workbench_review_accounting_resolution_repository import (
        SqlAlchemyReviewAccountingResolutionRepository,
    )
    from app.persistence.workbench_review_purchase_purpose_resolution_repository import (
        SqlAlchemyReviewPurchasePurposeResolutionRepository,
    )

    counts_before = _counts(session)
    facts = _operator_guidance_facts(
        review,
        COMPANY,
        purpose_repository=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        accounting_repository=SqlAlchemyReviewAccountingResolutionRepository(session),
        eligible_asset_account_ids=(),
        previous_invoice_reader=SqlAlchemyPreviousInvoiceDecisionReader(session),
    )
    assert facts.previous_invoice is not None
    assert facts.previous_invoice.latest.invoice_number == "I282026000000141"
    # Nothing is applied to the new review and nothing is persisted or cached anywhere.
    assert facts.current_purchase_purpose is None and facts.latest_accounting_resolution is None
    assert not session.new and not session.dirty
    assert _counts(session) == counts_before
    assert session.query(OperatingExpenseMappingRecord).count() == 0


def test_guidance_facts_skip_history_for_non_pending_reviews(session: Session) -> None:
    _completed(session, "review:cb")
    session.commit()

    class Boom:
        def list_completed_supplier_decisions(self, **kwargs: Any):
            raise AssertionError("must not be read")

    from app.composition.imports import _previous_invoice_guidance

    submitted = ReviewItem(
        review_id="review:new",
        invoice_id="inv-new",
        invoice_number=None,
        supplier_tax_number=CLOUDSPARK_VKN,
        supplier_name=None,
        invoice_date=None,
        currency=None,
        total_amount=None,
        workflow=WorkflowType.VENDOR_BILL,
        status=ReviewStatus.DECISION_SUBMITTED,
    )
    assert _previous_invoice_guidance(submitted, COMPANY, Boom(), None) is None


def _counts(db: Session) -> tuple[int, ...]:
    return tuple(
        db.query(model).count()
        for model in (
            WorkbenchReviewItem,
            WorkbenchReviewDecision,
            WorkflowExecution,
            WorkbenchReviewPurchasePurposeResolution,
            WorkbenchReviewAccountingResolution,
            OperatingExpenseMappingRecord,
        )
    )
