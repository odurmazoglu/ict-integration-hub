"""ADR-0013: Odoo Workbench operator request ingestion (Hub-pull adapter)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.application.execution import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.workbench.accounting_resolution import AccountingTreatmentType
from app.application.workbench.decision_ingestion import decision_idempotency_key
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import (
    PurchasePurposeEligibilityError,
    ReviewVersionConflictError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
)
from app.application.workbench.operator_request_handlers import (
    AccountingResolutionRequestHandler,
    DecisionRequestHandler,
    ExecuteVendorBillRequestHandler,
    PurchasePurposeRequestHandler,
    SupplierResolutionRequestHandler,
)
from app.application.workbench.operator_request_ingestion import (
    STALE_REQUEST_MESSAGE,
    UNAUTHORIZED_REQUEST_MESSAGE,
    OperatorActionContext,
    OperatorActionOutcome,
    OperatorActor,
    OperatorActorDirectory,
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestIngestionWorkflow,
    OperatorRequestLedgerEntry,
    OperatorRequestLedgerStatus,
    OperatorRequestOutcome,
    OperatorRequestReadFailure,
    operator_request_key,
)
from app.application.workbench.projection import OdooWorkbenchDecisionCandidate
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import WorkflowType
from app.composition.operator_requests import OperatorRequestTick, decision_mapping_for_requests
from app.core.config import Settings
from app.core.runtime_checks import runtime_configuration_errors
from app.db.base import Base
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.workbench_candidate_reader import (
    OdooWorkbenchAllocationFieldMapping,
    OdooWorkbenchFieldMapping,
    OdooWorkbenchParentFieldMapping,
)
from app.erp.odoo.workbench_operator_request_reader import (
    OdooOperatorRequestAcknowledger,
    OdooOperatorRequestFieldMapping,
    OdooOperatorRequestReader,
)
from app.models.workbench_operator_request import WorkbenchOperatorRequest
from app.persistence.workbench_operator_request_ledger import SqlAlchemyOperatorRequestLedger
from app.workers.uyumsoft_inbound_poller import PeriodicTask, PeriodicTaskScheduler

COMPANY = 1
REVIEW = "review:cloudspark"
REQUESTED_AT = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
OPERATOR = OperatorActor(actor="operator", permissions=frozenset({"workbench_review_decide", "workbench_execute"}))
DECIDER = OperatorActor(actor="decider", permissions=frozenset({"workbench_review_decide"}))


def _request(**overrides: Any) -> OperatorRequest:
    values: dict[str, Any] = {
        "odoo_record_id": 11,
        "review_id": REVIEW,
        "company_id": COMPANY,
        "action": OperatorRequestAction.PURCHASE_PURPOSE,
        "expected_version": 4,
        "requested_by_odoo_user_id": 2,
        "requested_at": REQUESTED_AT,
        "purchase_purpose": PurchasePurpose.INTERNAL_USE,
    }
    values.update(overrides)
    return OperatorRequest(**values)


# ---------------------------------------------------------------------- fakes


class FakeReader:
    def __init__(self, items: list[Any] | None = None, *, error: Exception | None = None) -> None:
        self.items = items or []
        self.error = error

    def list_pending(self, *, company_id: int, limit: int):
        if self.error is not None:
            raise self.error
        return tuple(self.items)


class FakeAcknowledger:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_times = fail_times

    def acknowledge(self, **kwargs: Any) -> bool:
        if self.fail_times:
            self.fail_times -= 1
            raise WorkbenchContractError("Odoo unreachable during acknowledgement.")
        self.calls.append(kwargs)
        return True


class FakeLedger:
    def __init__(self) -> None:
        self.rows: dict[str, OperatorRequestLedgerEntry] = {}
        self.commits = 0

    def find(self, request_key: str):
        return self.rows.get(request_key)

    def start(self, *, request_key: str, request: OperatorRequest, actor: str | None):
        entry = self.rows.get(request_key) or OperatorRequestLedgerEntry(
            request_key=request_key, status=OperatorRequestLedgerStatus.IN_PROGRESS, attempts=0
        )
        self.rows[request_key] = entry
        return entry

    def record_attempt(self, request_key: str, *, message: str):
        old = self.rows[request_key]
        self.rows[request_key] = OperatorRequestLedgerEntry(
            request_key=request_key,
            status=old.status,
            attempts=old.attempts + 1,
            message=message,
            authorization_id=old.authorization_id,
        )
        return self.rows[request_key]

    def record_authorization(self, request_key: str, *, authorization_id: str) -> None:
        old = self.rows[request_key]
        self.rows[request_key] = OperatorRequestLedgerEntry(
            request_key=request_key, status=old.status, attempts=old.attempts, authorization_id=authorization_id
        )

    def finish(self, request_key: str, *, status: OperatorRequestLedgerStatus, message: str) -> None:
        old = self.rows[request_key]
        self.rows[request_key] = OperatorRequestLedgerEntry(
            request_key=request_key,
            status=status,
            attempts=old.attempts,
            message=message,
            authorization_id=old.authorization_id,
        )

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


class FakeIssuer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def issue(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"auth-{len(self.calls)}"


class FakeRefresher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def sync(self, *, review_id: str, company_id: int):
        self.calls.append((review_id, company_id))


@dataclass
class ScriptedHandler:
    """Returns/raises the scripted results in order and records what it was asked."""

    script: list[Any]
    calls: list[OperatorRequest] = field(default_factory=list)
    authorizations: list[str] = field(default_factory=list)
    authorization_operation: str | None = None

    def handle(self, request: OperatorRequest, context: OperatorActionContext) -> OperatorActionOutcome:
        self.calls.append(request)
        if self.authorization_operation is not None:
            self.authorizations.append(context.ensure_authorization(self.authorization_operation))
        result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _workflow(
    reader: FakeReader,
    handler: ScriptedHandler,
    *,
    ledger: FakeLedger | None = None,
    acknowledger: FakeAcknowledger | None = None,
    issuer: FakeIssuer | None = None,
    refresher: FakeRefresher | None = None,
    actors: dict[int, OperatorActor] | None = None,
    action: OperatorRequestAction = OperatorRequestAction.PURCHASE_PURPOSE,
):
    return OperatorRequestIngestionWorkflow(
        reader=reader,
        acknowledger=acknowledger or FakeAcknowledger(),
        ledger=ledger or FakeLedger(),
        actors=OperatorActorDirectory({2: OPERATOR} if actors is None else actors),
        handlers={action: handler},
        authorization_issuer=issuer or FakeIssuer(),
        projection_refresher=refresher or FakeRefresher(),
        clock=lambda: datetime(2026, 10, 6, 9, 1, tzinfo=UTC),
        transient_errors=(ErpRepositoryError,),
    )


def _done(message: str = "Satın alma amacı kaydedildi.") -> OperatorActionOutcome:
    return OperatorActionOutcome(outcome=OperatorRequestOutcome.COMPLETED, message=message)


# ---------------------------------------------------------------------- workflow


def test_successful_request_runs_use_case_once_refreshes_projection_and_acknowledges() -> None:
    ledger, ack, refresher = FakeLedger(), FakeAcknowledger(), FakeRefresher()
    handler = ScriptedHandler([_done()])
    result = _workflow(FakeReader([_request()]), handler, ledger=ledger, acknowledger=ack, refresher=refresher).run(
        company_id=COMPANY
    )

    assert [r.outcome for r in result.results] == [OperatorRequestOutcome.COMPLETED]
    assert len(handler.calls) == 1
    assert refresher.calls == [(REVIEW, COMPANY)]
    assert ack.calls[0]["outcome"] is OperatorRequestOutcome.COMPLETED
    assert ack.calls[0]["requested_at"] == REQUESTED_AT
    assert ledger.rows[operator_request_key(_request())].status is OperatorRequestLedgerStatus.COMPLETED


def test_stale_expected_version_is_never_reinterpreted_and_shows_the_operator_message() -> None:
    handler = ScriptedHandler([ReviewVersionConflictError("The review version does not match expected_version.")])
    ack, refresher = FakeAcknowledger(), FakeRefresher()
    result = _workflow(FakeReader([_request()]), handler, acknowledger=ack, refresher=refresher).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.STALE
    assert ack.calls[0]["message"] == STALE_REQUEST_MESSAGE
    assert "Bu inceleme siz işlem yaparken değişti" in STALE_REQUEST_MESSAGE
    assert refresher.calls == [(REVIEW, COMPANY)]  # authoritative projection reloaded
    assert handler.calls[0].expected_version == 4  # passed through unchanged, once


def test_duplicate_observation_of_a_consumed_request_never_reexecutes() -> None:
    ledger = FakeLedger()
    handler = ScriptedHandler([_done()])
    workflow = _workflow(FakeReader([_request()]), handler, ledger=ledger)
    workflow.run(company_id=COMPANY)
    second = workflow.run(company_id=COMPANY)  # Odoo still shows the row (ack lag / replay)

    assert len(handler.calls) == 1
    assert second.results[0].outcome is OperatorRequestOutcome.COMPLETED
    assert second.results[0].acknowledged is True


def test_crash_after_hub_commit_before_odoo_ack_only_reacknowledges() -> None:
    ledger, ack = FakeLedger(), FakeAcknowledger(fail_times=1)
    handler = ScriptedHandler([_done()])
    workflow = _workflow(FakeReader([_request()]), handler, ledger=ledger, acknowledger=ack)

    first = workflow.run(company_id=COMPANY)
    assert first.results[0].acknowledged is False  # Hub committed; Odoo write failed
    second = workflow.run(company_id=COMPANY)

    assert len(handler.calls) == 1
    assert second.results[0].acknowledged is True
    assert ack.calls[0]["outcome"] is OperatorRequestOutcome.COMPLETED


def test_crash_after_use_case_commit_before_ledger_finish_resumes_through_use_case_replay() -> None:
    ledger = FakeLedger()
    key = operator_request_key(_request())
    ledger.start(request_key=key, request=_request(), actor="operator")  # left in_progress by a crash
    resumed = OperatorActionOutcome(outcome=OperatorRequestOutcome.ALREADY_COMPLETED, message="already")
    handler = ScriptedHandler([resumed])
    result = _workflow(FakeReader([_request()]), handler, ledger=ledger).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.ALREADY_COMPLETED
    assert ledger.rows[key].status is OperatorRequestLedgerStatus.COMPLETED


def test_resumed_write_reuses_the_recorded_authorization_and_never_issues_a_second() -> None:
    ledger, issuer = FakeLedger(), FakeIssuer()
    request = _request(action=OperatorRequestAction.EXECUTE_VENDOR_BILL, purchase_purpose=None)
    handler = ScriptedHandler(
        [ErpRepositoryError("Odoo timed out."), _done("ok")], authorization_operation="EXECUTE_VENDOR_BILL"
    )
    workflow = _workflow(
        FakeReader([request]), handler, ledger=ledger, issuer=issuer, action=OperatorRequestAction.EXECUTE_VENDOR_BILL
    )

    assert workflow.run(company_id=COMPANY).results[0].outcome is OperatorRequestOutcome.RETRY_LATER
    assert workflow.run(company_id=COMPANY).results[0].outcome is OperatorRequestOutcome.COMPLETED
    assert len(issuer.calls) == 1
    assert handler.authorizations == ["auth-1", "auth-1"]
    assert issuer.calls[0]["target_version"] == 4 and issuer.calls[0]["authorized_by"] == "operator"


def test_transient_failures_retry_without_acknowledging_then_close_as_failed() -> None:
    ack = FakeAcknowledger()
    handler = ScriptedHandler([ErpRepositoryError("down")] * 5)
    workflow = _workflow(FakeReader([_request()]), handler, acknowledger=ack)
    outcomes = [workflow.run(company_id=COMPANY).results[0].outcome for _ in range(5)]

    assert outcomes[:4] == [OperatorRequestOutcome.RETRY_LATER] * 4
    assert outcomes[4] is OperatorRequestOutcome.FAILED
    assert len(ack.calls) == 1 and ack.calls[0]["outcome"] is OperatorRequestOutcome.FAILED


def test_invalid_request_is_rejected_and_acknowledged_without_any_use_case() -> None:
    handler = ScriptedHandler([])
    ack = FakeAcknowledger()
    failure = OperatorRequestReadFailure(
        odoo_record_id=11, review_id=REVIEW, requested_at=REQUESTED_AT, message="İşlem türü seçilmelidir."
    )
    result = _workflow(FakeReader([failure]), handler, acknowledger=ack).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.REJECTED
    assert "İşlem türü seçilmelidir." in ack.calls[0]["message"]
    assert handler.calls == []


def test_unknown_odoo_user_is_unauthorized() -> None:
    handler = ScriptedHandler([])
    ack = FakeAcknowledger()
    result = _workflow(FakeReader([_request()]), handler, acknowledger=ack, actors={}).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.UNAUTHORIZED
    assert ack.calls[0]["message"] == UNAUTHORIZED_REQUEST_MESSAGE
    assert handler.calls == []


def test_execution_requires_the_existing_execute_permission() -> None:
    handler = ScriptedHandler([])
    request = _request(action=OperatorRequestAction.EXECUTE_VENDOR_BILL, purchase_purpose=None)
    result = _workflow(
        FakeReader([request]), handler, actors={2: DECIDER}, action=OperatorRequestAction.EXECUTE_VENDOR_BILL
    ).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.UNAUTHORIZED
    assert handler.calls == []


def test_writing_supplier_mode_without_execute_permission_is_rejected_before_any_write() -> None:
    issuer = FakeIssuer()
    request = _request(
        action=OperatorRequestAction.SUPPLIER_RESOLUTION,
        purchase_purpose=None,
        supplier_mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER,
    )
    use_case = RecordingAsyncUseCase(SimpleNamespace(already_applied=False))
    workflow = OperatorRequestIngestionWorkflow(
        reader=FakeReader([request]),
        acknowledger=FakeAcknowledger(),
        ledger=FakeLedger(),
        actors=OperatorActorDirectory({2: DECIDER}),
        handlers={OperatorRequestAction.SUPPLIER_RESOLUTION: SupplierResolutionRequestHandler(use_case=use_case)},
        authorization_issuer=issuer,
    )
    result = workflow.run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.REJECTED
    assert "workbench_execute" in result.results[0].message
    assert issuer.calls == [] and use_case.commands == []


def test_request_from_another_company_is_rejected() -> None:
    handler = ScriptedHandler([])
    result = _workflow(FakeReader([_request(company_id=2)]), handler).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.REJECTED
    assert handler.calls == []


def test_use_case_refusal_is_rejected_with_its_safe_message() -> None:
    handler = ScriptedHandler([PurchasePurposeEligibilityError("RESALE requires a product-shaped review.")])
    result = _workflow(FakeReader([_request()]), handler).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.REJECTED
    assert "RESALE requires a product-shaped review." in result.results[0].message


def test_unexpected_error_is_surfaced_as_failed_not_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    handler = ScriptedHandler([RuntimeError("boom")])
    result = _workflow(FakeReader([_request()]), handler).run(company_id=COMPANY)

    assert result.results[0].outcome is OperatorRequestOutcome.FAILED
    assert "RuntimeError" in result.results[0].message
    assert "workbench.operator_request.unexpected_error" in caplog.text


def test_unreadable_odoo_is_a_quiet_retry() -> None:
    result = _workflow(FakeReader(error=WorkbenchCandidateReadError("down")), ScriptedHandler([])).run(
        company_id=COMPANY
    )
    assert result.results == ()


def test_request_key_is_deterministic_and_changes_with_a_resubmission() -> None:
    assert operator_request_key(_request()) == operator_request_key(_request())
    assert operator_request_key(_request()) != operator_request_key(
        _request(requested_at=datetime(2026, 10, 6, 9, 5, tzinfo=UTC))
    )
    assert operator_request_key(_request()) != operator_request_key(_request(expected_version=5))


def test_request_shape_requires_the_action_inputs() -> None:
    with pytest.raises(WorkbenchContractError, match="Satın alma amacı"):
        _request(purchase_purpose=None)
    with pytest.raises(WorkbenchContractError, match="timezone-aware"):
        _request(requested_at=datetime(2026, 10, 6, 9, 0))


# ---------------------------------------------------------------------- actor directory


def test_actor_directory_parses_existing_permissions_only() -> None:
    directory = OperatorActorDirectory.from_json(
        '{"2": {"actor": "onur", "permissions": ["workbench_review_decide", "workbench_execute"]}}'
    )
    assert directory.resolve(2) == OperatorActor(
        actor="onur", permissions=frozenset({"workbench_review_decide", "workbench_execute"})
    )
    assert directory.resolve(3) is None
    assert OperatorActorDirectory.from_json(None).resolve(2) is None
    with pytest.raises(WorkbenchContractError, match="Unsupported"):
        OperatorActorDirectory.from_json('{"2": {"actor": "x", "permissions": ["admin"]}}')
    with pytest.raises(WorkbenchContractError):
        OperatorActorDirectory.from_json('{"abc": {"actor": "x", "permissions": []}}')


# ---------------------------------------------------------------------- handlers -> existing use cases


class RecordingAsyncUseCase:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.commands: list[Any] = []

    async def execute(self, command: Any) -> Any:
        self.commands.append(command)
        return self.result


class RecordingSyncUseCase(RecordingAsyncUseCase):
    def execute(self, command: Any) -> Any:  # type: ignore[override]
        self.commands.append(command)
        return self.result


def _context(actor: OperatorActor = OPERATOR, issued: list[str] | None = None) -> OperatorActionContext:
    issued = issued if issued is not None else []

    def ensure(operation: str) -> str:
        issued.append(operation)
        return "auth-1"

    return OperatorActionContext(actor=actor, ensure_authorization=ensure, trace_id="trace-1")


def test_supplier_match_existing_maps_to_existing_command_without_authorization() -> None:
    use_case = RecordingAsyncUseCase(SimpleNamespace(already_applied=False))
    issued: list[str] = []
    request = _request(
        action=OperatorRequestAction.SUPPLIER_RESOLUTION,
        purchase_purpose=None,
        supplier_mode=SupplierResolutionMode.MATCH_EXISTING,
        partner_id=451,
    )
    outcome = SupplierResolutionRequestHandler(use_case=use_case).handle(request, _context(issued=issued))

    command = use_case.commands[0]
    assert (command.mode, command.resolved_partner_id, command.expected_version) == (
        SupplierResolutionMode.MATCH_EXISTING,
        451,
        4,
    )
    assert command.approved_by == "operator" and command.authorization_id is None
    assert issued == [] and outcome.outcome is OperatorRequestOutcome.COMPLETED


@pytest.mark.parametrize(
    ("mode", "operation"),
    [
        (SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, "CREATE_PERMANENT_SUPPLIER"),
        (SupplierResolutionMode.ONE_OFF_VENDOR, "ONE_OFF_VENDOR_SUPPLIER"),
    ],
)
def test_writing_supplier_modes_use_the_existing_narrow_authorization(mode, operation) -> None:
    use_case = RecordingAsyncUseCase(SimpleNamespace(already_applied=True))
    issued: list[str] = []
    request = _request(action=OperatorRequestAction.SUPPLIER_RESOLUTION, purchase_purpose=None, supplier_mode=mode)
    outcome = SupplierResolutionRequestHandler(use_case=use_case).handle(request, _context(issued=issued))

    assert issued == [operation]
    assert use_case.commands[0].authorization_id == "auth-1"
    assert outcome.outcome is OperatorRequestOutcome.ALREADY_COMPLETED


def test_purchase_purpose_maps_to_existing_command() -> None:
    use_case = RecordingSyncUseCase(SimpleNamespace(already_applied=False))
    PurchasePurposeRequestHandler(use_case=use_case).handle(_request(note="iPhone"), _context())
    command = use_case.commands[0]
    assert (command.purchase_purpose, command.expected_version, command.approved_by, command.note) == (
        PurchasePurpose.INTERNAL_USE,
        4,
        "operator",
        "iPhone",
    )


def test_expense_accounting_forwards_only_expense_fields() -> None:
    use_case = RecordingAsyncUseCase(SimpleNamespace(already_applied=False))
    request = _request(
        action=OperatorRequestAction.ACCOUNTING_RESOLUTION,
        purchase_purpose=None,
        treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
        expense_account_id=247,
        expense_category="SOFTWARE",
        asset_account_id=74,  # an earlier fixed-asset choice still on the Odoo row
        depreciation_model_id=6,
    )
    AccountingResolutionRequestHandler(use_case=use_case).handle(request, _context())
    command = use_case.commands[0]
    assert (command.expense_account_id, command.expense_category) == (247, "SOFTWARE")
    assert (command.asset_account_id, command.depreciation_model_id) == (None, None)


def test_fixed_asset_accounting_forwards_only_asset_fields() -> None:
    use_case = RecordingAsyncUseCase(SimpleNamespace(already_applied=False))
    request = _request(
        action=OperatorRequestAction.ACCOUNTING_RESOLUTION,
        purchase_purpose=None,
        treatment_type=AccountingTreatmentType.CAPITALIZE_FIXED_ASSET,
        expense_account_id=247,
        expense_category="OLD",
        asset_account_id=91,
        depreciation_model_id=12,
    )
    outcome = AccountingResolutionRequestHandler(use_case=use_case).handle(request, _context())
    command = use_case.commands[0]
    assert command.treatment_type is AccountingTreatmentType.CAPITALIZE_FIXED_ASSET
    assert (command.asset_account_id, command.depreciation_model_id) == (91, 12)
    assert (command.expense_account_id, command.expense_category) == (None, None)
    assert outcome.outcome is OperatorRequestOutcome.COMPLETED


def _candidate(**overrides: Any) -> OdooWorkbenchDecisionCandidate:
    values: dict[str, Any] = {
        "odoo_record_id": 11,
        "review_id": REVIEW,
        "company_id": COMPANY,
        "expected_version": 4,
        "decision": ReviewDecisionType.SELECT_WORKFLOW,
        "idempotency_key": None,
        "decided_by_odoo_user_id": 2,
        "decided_at": REQUESTED_AT,
        "decision_ready": True,
        "selected_workflow": WorkflowType.VENDOR_BILL,
    }
    values.update(overrides)
    return OdooWorkbenchDecisionCandidate(**values)


class FakeDecisionReader:
    def __init__(self, candidate: OdooWorkbenchDecisionCandidate) -> None:
        self.candidate = candidate

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate:
        return self.candidate


class FakeValidator:
    def __init__(self) -> None:
        self.calls = 0

    def validate(self, candidate, *, requested_company_id: int):
        self.calls += 1


class FakeSubmitter:
    def __init__(self, *, matching: bool = False) -> None:
        self.commands: list[Any] = []
        self.matching = matching

    def has_matching_decision(self, command) -> bool:
        return self.matching

    def execute(self, command):
        self.commands.append(command)
        return SimpleNamespace(version=5)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _decision_request() -> OperatorRequest:
    return _request(action=OperatorRequestAction.DECISION, purchase_purpose=None)


def test_decision_uses_existing_candidate_parsing_validation_and_idempotency_key() -> None:
    submitter, uow, validator = FakeSubmitter(), FakeUnitOfWork(), FakeValidator()
    candidate = _candidate()
    outcome = DecisionRequestHandler(
        candidate_reader=FakeDecisionReader(candidate),
        erp_reference_validator=validator,
        decision_submitter=submitter,
        unit_of_work=uow,
    ).handle(_decision_request(), _context())

    command = submitter.commands[0]
    assert command.idempotency_key == decision_idempotency_key(candidate)
    assert command.expected_version == 4 and command.decided_by == "odoo:2"
    assert validator.calls == 1 and uow.commits == 1
    assert outcome.outcome is OperatorRequestOutcome.COMPLETED and "v5" in outcome.message


def test_duplicate_decision_reports_already_completed() -> None:
    outcome = DecisionRequestHandler(
        candidate_reader=FakeDecisionReader(_candidate()),
        erp_reference_validator=FakeValidator(),
        decision_submitter=FakeSubmitter(matching=True),
        unit_of_work=FakeUnitOfWork(),
    ).handle(_decision_request(), _context())
    assert outcome.outcome is OperatorRequestOutcome.ALREADY_COMPLETED


def test_decision_row_that_changed_since_the_scan_is_stale() -> None:
    submitter = FakeSubmitter()
    with pytest.raises(ReviewVersionConflictError):
        DecisionRequestHandler(
            candidate_reader=FakeDecisionReader(_candidate(expected_version=5)),
            erp_reference_validator=FakeValidator(),
            decision_submitter=submitter,
            unit_of_work=FakeUnitOfWork(),
        ).handle(_decision_request(), _context())
    assert submitter.commands == []


class FakeDispatcher:
    def __init__(self, status: WorkbenchVendorBillExecutionStatus, *, message: str | None = None) -> None:
        self.status = status
        self.message = message
        self.calls: list[dict[str, Any]] = []

    def execute(self, **kwargs: Any):
        self.calls.append(kwargs)
        artifacts = (
            (
                ExecutionArtifact(
                    artifact_type=ExecutionArtifactType.VENDOR_BILL,
                    artifact_id="69",
                    external_identity="vendor-bill-write:test",
                    created=True,
                ),
            )
            if self.status
            in (WorkbenchVendorBillExecutionStatus.EXECUTED, WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED)
            else ()
        )
        return SimpleNamespace(status=self.status, artifacts=artifacts, message=self.message)


def _execute_request() -> OperatorRequest:
    return _request(action=OperatorRequestAction.EXECUTE_VENDOR_BILL, purchase_purpose=None)


def test_execution_issues_narrow_authorization_and_runs_existing_dispatcher_in_execute_mode() -> None:
    dispatcher, issued = FakeDispatcher(WorkbenchVendorBillExecutionStatus.EXECUTED), []
    outcome = ExecuteVendorBillRequestHandler(dispatcher=dispatcher).handle(_execute_request(), _context(issued=issued))

    call = dispatcher.calls[0]
    assert issued == ["EXECUTE_VENDOR_BILL"]
    assert call["mode"] is ExecutionMode.EXECUTE and call["decision_version"] == 4
    assert call["approval"].approved_by == "operator" and call["authorization_id"] == "auth-1"
    assert outcome.outcome is OperatorRequestOutcome.COMPLETED and "69" in outcome.message


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED, OperatorRequestOutcome.ALREADY_COMPLETED),
        (WorkbenchVendorBillExecutionStatus.NOT_FOUND, OperatorRequestOutcome.STALE),
        (WorkbenchVendorBillExecutionStatus.EXECUTION_DISABLED, OperatorRequestOutcome.REJECTED),
    ],
)
def test_execution_statuses_map_to_operator_outcomes(status, expected) -> None:
    outcome = ExecuteVendorBillRequestHandler(dispatcher=FakeDispatcher(status, message="gate closed")).handle(
        _execute_request(), _context()
    )
    assert outcome.outcome is expected


# ---------------------------------------------------------------------- Odoo adapter

MODEL = "x_ipp_import_workbench"


def _request_mapping() -> OdooOperatorRequestFieldMapping:
    return OdooOperatorRequestFieldMapping(
        model=MODEL,
        review_id="x_studio_review_id",
        company_id="x_studio_company",
        ready="x_studio_ipp_req_ready",
        action="x_studio_ipp_req_action",
        expected_version="x_studio_ipp_req_version",
        requested_by="x_studio_ipp_req_requested_by",
        requested_at="x_studio_ipp_req_requested_at",
        result="x_studio_ipp_req_result",
        message="x_studio_ipp_req_message",
        processed_at="x_studio_ipp_req_processed_at",
        supplier_mode="x_studio_ipp_req_supplier_mode",
        partner="x_studio_ipp_req_partner",
        purchase_purpose="x_studio_ipp_req_purpose",
        treatment="x_studio_ipp_req_treatment",
        expense_account="x_studio_ipp_req_expense_account",
        expense_category="x_studio_ipp_req_expense_category",
        asset_account="x_studio_ipp_req_asset_account",
        depreciation_model="x_studio_ipp_req_depreciation_model",
        note="x_studio_ipp_req_note",
    )


class FakeJson2:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.domains: list[list[Any]] = []
        self.writes: list[tuple[int, dict[str, Any]]] = []

    def search_read(self, *, model: str, domain: list[Any], fields: list[str], limit: int, offset: int = 0):
        assert model == MODEL
        self.domains.append(domain)
        if domain and domain[0][0] == "id":
            return tuple(row for row in self.rows if row["id"] == domain[0][2])
        return tuple(self.rows)

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        self.writes.append((record_id, values))


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 28,
        "x_studio_review_id": "review:apple",
        "x_studio_company": [1, "ICT"],
        "x_studio_ipp_req_ready": True,
        "x_studio_ipp_req_action": "Muhasebe İşlemi",
        "x_studio_ipp_req_version": 4,
        "x_studio_ipp_req_requested_by": [2, "Operator"],
        "x_studio_ipp_req_requested_at": "2026-10-06 09:00:00",
        "x_studio_ipp_req_supplier_mode": False,
        "x_studio_ipp_req_partner": False,
        "x_studio_ipp_req_purpose": False,
        "x_studio_ipp_req_treatment": "Sabit Kıymet / Demirbaş",
        "x_studio_ipp_req_expense_account": False,
        "x_studio_ipp_req_expense_category": False,
        "x_studio_ipp_req_asset_account": [91, "255000 Demirbaşlar"],
        "x_studio_ipp_req_depreciation_model": [12, "3 Year Linear No Prorata"],
        "x_studio_ipp_req_note": False,
    }
    row.update(overrides)
    return row


def test_reader_discovers_only_ready_rows_of_the_company_and_parses_studio_labels() -> None:
    adapter = FakeJson2([_row()])
    (request,) = OdooOperatorRequestReader(adapter=adapter, mapping=_request_mapping()).list_pending(
        company_id=1, limit=10
    )

    assert adapter.domains[0] == [["x_studio_company", "=", 1], ["x_studio_ipp_req_ready", "=", True]]
    assert isinstance(request, OperatorRequest)
    assert request.action is OperatorRequestAction.ACCOUNTING_RESOLUTION
    assert request.treatment_type is AccountingTreatmentType.CAPITALIZE_FIXED_ASSET
    assert (request.asset_account_id, request.depreciation_model_id) == (91, 12)
    assert request.requested_at == REQUESTED_AT and request.requested_by_odoo_user_id == 2


def test_reader_reports_malformed_rows_instead_of_guessing() -> None:
    adapter = FakeJson2([_row(x_studio_ipp_req_action="Toplu Onay"), _row(id=29, x_studio_ipp_req_requested_at=False)])
    first, second = OdooOperatorRequestReader(adapter=adapter, mapping=_request_mapping()).list_pending(
        company_id=1, limit=10
    )
    assert isinstance(first, OperatorRequestReadFailure) and "desteklenmiyor" in first.message
    assert isinstance(second, OperatorRequestReadFailure) and "İşleme Gönder" in second.message


def test_acknowledger_clears_ready_only_for_the_same_request() -> None:
    adapter = FakeJson2([_row()])
    ack = OdooOperatorRequestAcknowledger(adapter=adapter, mapping=_request_mapping())
    processed = datetime(2026, 10, 6, 9, 1, tzinfo=UTC)

    assert ack.acknowledge(
        odoo_record_id=28,
        requested_at=REQUESTED_AT,
        outcome=OperatorRequestOutcome.STALE,
        message=STALE_REQUEST_MESSAGE,
        processed_at=processed,
    )
    record_id, values = adapter.writes[0]
    assert record_id == 28
    assert values == {
        "x_studio_ipp_req_result": "Güncel Değil",
        "x_studio_ipp_req_message": STALE_REQUEST_MESSAGE,
        "x_studio_ipp_req_processed_at": "2026-10-06 09:01:00",
        "x_studio_ipp_req_ready": False,
    }

    # The operator re-submitted meanwhile: a newer request must never be cleared.
    newer = FakeJson2([_row(x_studio_ipp_req_requested_at="2026-10-06 09:00:30")])
    assert not OdooOperatorRequestAcknowledger(adapter=newer, mapping=_request_mapping()).acknowledge(
        odoo_record_id=28,
        requested_at=REQUESTED_AT,
        outcome=OperatorRequestOutcome.COMPLETED,
        message="ok",
        processed_at=processed,
    )
    assert newer.writes == []


def test_acknowledger_never_writes_authoritative_projection_fields() -> None:
    adapter = FakeJson2([_row()])
    OdooOperatorRequestAcknowledger(adapter=adapter, mapping=_request_mapping()).acknowledge(
        odoo_record_id=28,
        requested_at=REQUESTED_AT,
        outcome=OperatorRequestOutcome.COMPLETED,
        message="ok",
        processed_at=REQUESTED_AT,
    )
    assert all(name.startswith("x_studio_ipp_req_") for name in adapter.writes[0][1])


def test_decision_reader_mapping_points_at_the_request_snapshot_fields() -> None:
    base = OdooWorkbenchFieldMapping(
        parent=OdooWorkbenchParentFieldMapping(
            model=MODEL,
            review_id="x_studio_review_id",
            company_id="x_studio_company",
            expected_version="x_studio_review_version",
            decision="x_studio_decision",
            selected_workflow="x_studio_selected_workflow",
            decision_ready="x_studio_ready_for_hub_processing",
            decided_at="x_studio_decided_at",
            decided_by="x_studio_decided_by",
            idempotency_key="x_studio_decision_idempotency_key",
            allocation_one2many_field="x_studio_allocation_list",
            invoice_total="x_studio_invoice_total",
            currency="x_studio_currency",
        ),
        allocation=OdooWorkbenchAllocationFieldMapping(
            model="x_ipp_review_allocatio",
            parent_many2one_field="x_studio_review",
            allocation_key="x_studio_key",
            allocation_type="x_studio_type",
            amount="x_studio_amount",
            percentage="x_studio_percentage",
            currency="x_studio_currency",
        ),
    )
    mapped = decision_mapping_for_requests(base, _request_mapping())

    # The projected version is never the expected version of an operator decision.
    assert mapped.parent.expected_version == "x_studio_ipp_req_version"
    assert mapped.parent.decision_ready == "x_studio_ipp_req_ready"
    assert mapped.parent.decided_by == "x_studio_ipp_req_requested_by"
    assert mapped.parent.decided_at == "x_studio_ipp_req_requested_at"
    assert mapped.parent.decision == "x_studio_decision"  # existing decision inputs reused


# ---------------------------------------------------------------------- ledger persistence


@pytest.fixture
def ledger_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[WorkbenchOperatorRequest.__table__])
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_sql_ledger_lifecycle_and_authorization_record(ledger_session: Session) -> None:
    ledger = SqlAlchemyOperatorRequestLedger(ledger_session)
    request = _request()
    key = operator_request_key(request)

    entry = ledger.start(request_key=key, request=request, actor="operator")
    ledger.commit()
    assert entry.status is OperatorRequestLedgerStatus.IN_PROGRESS and not entry.terminal
    assert ledger.start(request_key=key, request=request, actor="operator").request_key == key  # idempotent

    ledger.record_authorization(key, authorization_id="11111111-1111-1111-1111-111111111111")
    ledger.record_attempt(key, message="timeout")
    ledger.finish(key, status=OperatorRequestLedgerStatus.COMPLETED, message="done")
    ledger.commit()

    stored = ledger.find(key)
    assert stored is not None and stored.terminal
    assert (stored.status, stored.attempts, stored.authorization_id) == (
        OperatorRequestLedgerStatus.COMPLETED,
        1,
        "11111111-1111-1111-1111-111111111111",
    )
    with pytest.raises(WorkbenchContractError):
        ledger.record_authorization(key, authorization_id="22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------- scheduler / settings


def test_periodic_scheduler_runs_each_task_and_survives_a_crashing_one() -> None:
    calls: list[str] = []

    def crashing() -> None:
        calls.append("requests")
        raise RuntimeError("boom")

    scheduler = PeriodicTaskScheduler(
        tasks=[PeriodicTask("uyumsoft", lambda: calls.append("uyumsoft"), 180), PeriodicTask("requests", crashing, 60)],
        stop_event=threading.Event(),
    )
    assert scheduler.run(max_rounds=1) == 2
    assert calls == ["uyumsoft", "requests"]


def test_worker_runs_the_operator_request_tick_without_uyumsoft_polling() -> None:
    from app.workers import uyumsoft_inbound_poller

    ticks: list[str] = []
    settings = Settings(
        odoo_workbench_operator_requests_enabled=True,
        odoo_workbench_operator_requests_company_id=1,
        odoo_workbench_projection_publish_enabled=True,
        uyumsoft_inbound_poll_enabled=False,
    )
    exit_code = uyumsoft_inbound_poller.main(
        ["--once"],
        settings=settings,
        stop_event=threading.Event(),
        cycle_builder=lambda _settings: pytest.fail("Uyumsoft polling is disabled"),
        operator_request_tick_builder=lambda _settings: lambda: ticks.append("tick"),
    )
    assert exit_code == 0 and ticks == ["tick"]


def test_operator_requests_need_explicit_company_and_live_projection() -> None:
    settings = Settings(odoo_workbench_operator_requests_enabled=True)
    errors = runtime_configuration_errors(settings)
    assert any("ODOO_WORKBENCH_OPERATOR_REQUESTS_COMPANY_ID" in error for error in errors)
    assert any("ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED" in error for error in errors)


def test_tick_is_single_flight_and_closes_its_sessions() -> None:
    from contextlib import contextmanager

    closed: list[str] = []

    class Busy:
        @contextmanager
        def hold(self):
            yield False

    class Free:
        @contextmanager
        def hold(self):
            yield True

    class S:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    names = iter(["business", "ledger"])

    def factory():
        return S(next(names))

    workflow = SimpleNamespace(run=lambda company_id: f"ran:{company_id}")
    assert OperatorRequestTick(session_factory=factory, lock=Busy(), company_id=1, workflow_factory=None).run() is None
    tick = OperatorRequestTick(
        session_factory=factory, lock=Free(), company_id=1, workflow_factory=lambda _business, _ledger: workflow
    )
    assert tick.run() == "ran:1"
    assert closed == ["business", "ledger"]
