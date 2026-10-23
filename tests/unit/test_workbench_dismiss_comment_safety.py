"""Workbench DISMISS safety: a new dismissal needs an explanation; wording is never a statutory rejection.

DISMISS is a Hub accounting-processing decision only. It is terminal, keeps the source
evidence and never creates a Vendor Bill. These tests pin the comment requirement at the
one boundary every new decision passes (``SubmitReviewDecisionUseCase``) through each
public entry point, and the operator wording that must not read as an e-Fatura "ret".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_request_context, get_submit_review_decision_use_case
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.workbench import (
    OdooWorkbenchDecisionCandidate,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewDetailQuery,
    ReviewStatus,
    WorkbenchDecisionIngestionStatus,
    WorkbenchDecisionIngestionWorkflow,
    decision_idempotency_key,
)
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.exceptions import (
    DismissCommentRequiredError,
    ReviewDecisionIdempotencyConflictError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
)
from app.application.workbench.operator_guidance import (
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
)
from app.application.workbench.operator_request_handlers import DecisionRequestHandler
from app.application.workbench.operator_request_ingestion import (
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestOutcome,
    _classified_application_error,
)
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.main import app
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.import_receipt import ImportReceipt
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewRepository
from app.persistence.import_history import SqlAlchemyImportHistory
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.unit.test_workbench_review_persistence import _review_item

COMPANY_ID = 7
REVIEW_ID = "review-1"
REASON = "Kişisel harcama — şahsi ödeme"
NOT_STATUTORY = "yasal e-Fatura reddi veya iptali değildir"


@pytest.fixture()
def session() -> Session:
    # One shared connection: the REST handler runs in a worker thread.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
            ImportReceipt.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def _seed(session: Session, review_id: str = REVIEW_ID) -> SqlAlchemyReviewRepository:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item(review_id), company_id=COMPANY_ID, idempotency_key=f"import:{review_id}")
    session.commit()
    return repository


def _use_case(session: Session) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _dismiss(
    comment: str | None,
    *,
    expected_version: int = 1,
    idempotency_key: str = "dismiss-key-1",
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=expected_version,
        decision=ReviewDecisionType.DISMISS,
        decided_by="finance.user",
        idempotency_key=idempotency_key,
        comment=comment,
    )


def _status(session: Session) -> tuple[str, int]:
    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == REVIEW_ID))
    return item.status, item.version


def _decision_count(session: Session) -> int:
    return session.query(WorkbenchReviewDecision).count()


# ----------------------------------------------------------------- use case boundary


@pytest.mark.parametrize("comment", [None, "", "   ", "\n\t "])
def test_new_dismiss_without_meaningful_comment_is_refused_without_mutation(session: Session, comment) -> None:
    _seed(session)

    with pytest.raises(DismissCommentRequiredError) as error:
        _use_case(session).execute(_dismiss(comment))

    assert error.value.error_category == "dismiss_comment_required"
    assert _status(session) == (ReviewStatus.PENDING_REVIEW.value, 1)
    assert _decision_count(session) == 0


def test_dismiss_comment_is_trimmed_and_persisted_with_the_decision(session: Session) -> None:
    _seed(session)

    acknowledgement = _use_case(session).execute(_dismiss(f"  {REASON}  "))

    assert acknowledgement.status is ReviewStatus.DISMISSED
    assert acknowledgement.version == 2
    record = session.scalar(select(WorkbenchReviewDecision))
    assert record.comment == REASON
    assert record.decision_type == "dismiss"
    assert record.decided_by == "finance.user"
    assert _status(session) == (ReviewStatus.DISMISSED.value, 2)


@pytest.mark.parametrize("comment", [None, "", "  "])
def test_select_workflow_still_accepts_an_optional_comment(session: Session, comment) -> None:
    _seed(session)
    command = ReviewDecisionCommand(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.RFQ,
        decided_by="finance.user",
        idempotency_key="select-key-1",
        comment=comment,
    )

    acknowledgement = _use_case(session).execute(command)

    assert acknowledgement.status is ReviewStatus.DECISION_SUBMITTED
    # Never trimmed or otherwise rewritten: select_workflow fingerprints stay byte-identical.
    assert session.scalar(select(WorkbenchReviewDecision)).comment == comment


def test_dismiss_replay_returns_the_same_acknowledgement(session: Session) -> None:
    _seed(session)
    first = _use_case(session).execute(_dismiss(REASON))

    # The same request again (including the untrimmed form the operator may resend).
    second = _use_case(session).execute(_dismiss(REASON))
    third = _use_case(session).execute(_dismiss(f" {REASON} "))

    assert second == first
    assert third == first
    assert _decision_count(session) == 1
    assert _status(session) == (ReviewStatus.DISMISSED.value, 2)


def test_dismiss_idempotency_key_reused_with_a_different_comment_conflicts(session: Session) -> None:
    _seed(session)
    _use_case(session).execute(_dismiss(REASON))

    with pytest.raises(ReviewDecisionIdempotencyConflictError):
        _use_case(session).execute(_dismiss("Başka bir neden"))

    assert _decision_count(session) == 1


def test_dismiss_with_stale_version_conflicts_without_mutation(session: Session) -> None:
    _seed(session)

    with pytest.raises(ReviewVersionConflictError):
        _use_case(session).execute(_dismiss(REASON, expected_version=5))

    assert _status(session) == (ReviewStatus.PENDING_REVIEW.value, 1)
    assert _decision_count(session) == 0


def test_dismissed_review_refuses_any_further_decision(session: Session) -> None:
    _seed(session)
    _use_case(session).execute(_dismiss(REASON))

    with pytest.raises(ReviewStateConflictError):
        _use_case(session).execute(
            _dismiss("Tekrar dışı bırakma denemesi", expected_version=2, idempotency_key="dismiss-key-2")
        )

    assert _decision_count(session) == 1


def test_historical_dismiss_without_comment_stays_readable_and_replayable(session: Session) -> None:
    # A decision persisted before this rule (direct repository write, as in history).
    repository = _seed(session)
    repository.submit_review_decision(_dismiss(None))
    session.commit()

    item = repository.get_review_item(ReviewDetailQuery(review_id=REVIEW_ID, company_id=COMPANY_ID))
    accepted = repository.get_accepted_decision(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2)
    replay = _use_case(session).execute(_dismiss(None))

    assert item.status is ReviewStatus.DISMISSED
    assert accepted.decision_type is ReviewDecisionType.DISMISS
    assert session.scalar(select(WorkbenchReviewDecision)).comment is None
    assert replay.status is ReviewStatus.DISMISSED
    assert replay.version == 2
    assert _decision_count(session) == 1


# ----------------------------------------------------------------- REST


def _context() -> RequestContext:
    return RequestContext(
        user_id="user-1",
        user_name="Finance User",
        company_id=COMPANY_ID,
        permissions=(Permission.WORKBENCH_REVIEW_DECIDE,),
        trace_id="trace-dismiss",
        authentication_method=AuthenticationMethod.JWT,
    )


async def _post(api_client: AsyncClient, session: Session, body: dict[str, Any]):
    app.dependency_overrides[get_request_context] = _context
    app.dependency_overrides[get_submit_review_decision_use_case] = lambda: _use_case(session)
    try:
        return await api_client.post(f"/api/workbench/reviews/{REVIEW_ID}/decision", json=body)
    finally:
        app.dependency_overrides.clear()


def _dismiss_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"expected_version": 1, "decision": "dismiss", "idempotency_key": "rest-key-1"}
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_dismiss_body(), id="missing"),
        pytest.param(_dismiss_body(comment=None), id="null"),
        pytest.param(_dismiss_body(comment=""), id="empty"),
        pytest.param(_dismiss_body(comment="   "), id="whitespace"),
    ],
)
async def test_rest_dismiss_without_meaningful_comment_is_422(api_client: AsyncClient, session: Session, body) -> None:
    _seed(session)

    response = await _post(api_client, session, body)

    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["code"] == "dismiss_comment_required"
    assert "gerekçe" in error["message"]
    assert _status(session) == (ReviewStatus.PENDING_REVIEW.value, 1)
    assert _decision_count(session) == 0


async def test_rest_dismiss_with_valid_comment_is_accepted(api_client: AsyncClient, session: Session) -> None:
    _seed(session)

    response = await _post(api_client, session, _dismiss_body(comment=f" {REASON} "))

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "dismissed"
    record = session.scalar(select(WorkbenchReviewDecision))
    assert record.comment == REASON
    assert record.decided_by == "user-1"


async def test_rest_dismiss_still_requires_the_decide_permission(api_client: AsyncClient, session: Session) -> None:
    _seed(session)
    app.dependency_overrides[get_request_context] = lambda: RequestContext(
        user_id="user-1",
        user_name="Finance User",
        company_id=COMPANY_ID,
        permissions=(Permission.WORKBENCH_REVIEW_READ,),
        trace_id="trace-dismiss",
        authentication_method=AuthenticationMethod.JWT,
    )
    app.dependency_overrides[get_submit_review_decision_use_case] = lambda: _use_case(session)
    try:
        response = await api_client.post(
            f"/api/workbench/reviews/{REVIEW_ID}/decision", json=_dismiss_body(comment=REASON)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert _decision_count(session) == 0


# ----------------------------------------------------------------- Odoo Workbench decision sync


def _candidate(comment: str | None) -> OdooWorkbenchDecisionCandidate:
    return OdooWorkbenchDecisionCandidate(
        odoo_record_id=42,
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=1,
        decision=ReviewDecisionType.DISMISS,
        decided_by_odoo_user_id=11,
        decided_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
        decision_ready=True,
        comment=comment,
    )


class _Reader:
    def __init__(self, candidate: OdooWorkbenchDecisionCandidate) -> None:
        self.candidate = candidate

    def list_ready_decision_results(self, *, company_id: int, limit: int) -> tuple[object, ...]:
        return (self.candidate,)

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate:
        return self.candidate


class _Validator:
    def validate(self, candidate, *, requested_company_id: int):
        return candidate


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def acknowledge_decision(self, acknowledgement, **kwargs: Any) -> object:
        self.calls.append({"acknowledgement": acknowledgement, **kwargs})
        return object()


def _sync(session: Session, candidate: OdooWorkbenchDecisionCandidate, publisher: _Publisher):
    workflow = WorkbenchDecisionIngestionWorkflow(
        candidate_reader=_Reader(candidate),
        erp_reference_validator=_Validator(),
        decision_submitter=_use_case(session),
        acknowledgement_publisher=publisher,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        idempotency_key_factory=decision_idempotency_key,
    )
    return workflow.sync_ready_decisions(company_id=COMPANY_ID, trace_id="trace-sync")


@pytest.mark.parametrize("comment", [None, "", "  "])
def test_odoo_dismiss_without_comment_is_refused_non_destructively(session: Session, comment) -> None:
    _seed(session)
    publisher = _Publisher()

    result = _sync(session, _candidate(comment), publisher)

    candidate_result = result.results[0]
    assert candidate_result.status is WorkbenchDecisionIngestionStatus.INVALID_DECISION
    assert candidate_result.acknowledged is False
    assert "gerekçe" in (candidate_result.message or "")
    assert result.failed_count == 1
    # Nothing persisted and Odoo is not acknowledged: the row stays as the operator left it.
    assert publisher.calls == []
    assert _status(session) == (ReviewStatus.PENDING_REVIEW.value, 1)
    assert _decision_count(session) == 0


def test_odoo_dismiss_with_comment_is_accepted_and_acknowledged(session: Session) -> None:
    _seed(session)
    publisher = _Publisher()

    result = _sync(session, _candidate(REASON), publisher)

    assert result.results[0].status is WorkbenchDecisionIngestionStatus.PROCESSED
    assert publisher.calls[0]["acknowledgement"].status is ReviewStatus.DISMISSED
    record = session.scalar(select(WorkbenchReviewDecision))
    assert record.comment == REASON
    assert record.decided_by == "odoo:11"


# ----------------------------------------------------------------- ADR-0013 operator "Karar" request


def _operator_request() -> OperatorRequest:
    return OperatorRequest(
        odoo_record_id=42,
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        action=OperatorRequestAction.DECISION,
        expected_version=1,
        requested_by_odoo_user_id=11,
        requested_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
    )


def _handler(session: Session, comment: str | None) -> DecisionRequestHandler:
    return DecisionRequestHandler(
        candidate_reader=_Reader(_candidate(comment)),
        erp_reference_validator=_Validator(),
        decision_submitter=_use_case(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def test_operator_dismiss_request_without_comment_is_rejected_with_an_actionable_message(session: Session) -> None:
    _seed(session)

    with pytest.raises(DismissCommentRequiredError) as error:
        _handler(session, None).handle(_operator_request(), None)  # type: ignore[arg-type]
    outcome = _classified_application_error(error.value)

    assert outcome.outcome is OperatorRequestOutcome.REJECTED
    assert "gerekçe zorunludur" in outcome.message
    assert _status(session) == (ReviewStatus.PENDING_REVIEW.value, 1)
    assert _decision_count(session) == 0


def test_operator_dismiss_request_with_comment_completes(session: Session) -> None:
    _seed(session)

    outcome = _handler(session, REASON).handle(_operator_request(), None)  # type: ignore[arg-type]

    assert outcome.outcome is OperatorRequestOutcome.COMPLETED
    assert _status(session) == (ReviewStatus.DISMISSED.value, 2)


# ----------------------------------------------------------------- re-import


def test_reimport_of_a_dismissed_invoice_is_already_imported_and_does_not_reopen(session: Session) -> None:
    _seed(session)
    _use_case(session).execute(_dismiss(REASON))

    existing = SqlAlchemyImportHistory(session).find_imported_invoice(f"import:{REVIEW_ID}")

    assert existing is not None
    assert existing.status == "already_imported"
    assert _status(session) == (ReviewStatus.DISMISSED.value, 2)
    assert session.query(WorkbenchReviewItem).count() == 1


# ----------------------------------------------------------------- operator wording


def _guidance_input(
    *,
    status: ReviewStatus,
    codes: tuple[ManualReviewReasonCode, ...] = (),
    decision_type: ReviewDecisionType | None = None,
) -> GuidanceInput:
    return GuidanceInput(
        status=status,
        reason_codes=codes,
        supplier_name="Supplier",
        decision_type=decision_type,
        decision_workflow=None,
        decision_version=2 if decision_type is not None else None,
        execution_state=None,
        vendor_bill_id=None,
    )


def _all_text(guidance) -> str:
    return guidance.todo_html + guidance.completed_html


def test_dismissed_review_guidance_states_hub_exclusion_and_not_a_statutory_rejection() -> None:
    guidance = build_operator_guidance(
        _guidance_input(status=ReviewStatus.DISMISSED, decision_type=ReviewDecisionType.DISMISS),
        OperatorGuidanceFacts(),
    )

    text = _all_text(guidance)
    assert guidance.next_action is OperatorNextAction.DONE
    assert "Muhasebeleştirme dışı bırakıldı" in text
    assert "Hub bu belge için Vendor Bill oluşturmaz" in text
    assert NOT_STATUTORY in text
    assert "reddedildi" not in text.lower()


@pytest.mark.parametrize(
    "code",
    [ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED, ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED],
)
def test_return_and_withholding_guidance_offer_dismissal_without_implying_rejection(code) -> None:
    guidance = build_operator_guidance(
        _guidance_input(status=ReviewStatus.PENDING_REVIEW, codes=(code,)), OperatorGuidanceFacts()
    )

    text = _all_text(guidance)
    assert "Muhasebeleştirme Dışı Bırak" in text
    assert "Karar Notu alanına" in text
    assert NOT_STATUTORY in text
    assert "reddedin" not in text.lower()
