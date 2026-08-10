from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    LineResolution,
    OdooWorkbenchDecisionCandidate,
    OdooWorkbenchSubmissionStatus,
    ReviewDecisionAcknowledgement,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewStatus,
    SubmitOdooWorkbenchCandidateCommand,
    SubmitOdooWorkbenchCandidateUseCase,
    TaxResolution,
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateNotFoundError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
    WorkbenchErpReferenceNotFoundError,
    WorkbenchSubmissionCompanyMismatchError,
    WorkbenchSubmissionOrchestrationError,
)
from app.application.workbench.exceptions import (
    ReviewDecisionIdempotencyConflictError,
    ReviewVersionConflictError,
)
from app.application.workflow import WorkflowType


def test_submit_odoo_workbench_candidate_command_validates_input_and_is_immutable() -> None:
    command = SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7)

    assert command.review_id == "review-1"
    with pytest.raises(FrozenInstanceError):
        command.review_id = "changed"  # type: ignore[misc]
    with pytest.raises(WorkbenchContractError):
        SubmitOdooWorkbenchCandidateCommand(review_id="", company_id=7)
    with pytest.raises(WorkbenchContractError):
        SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=0)
    with pytest.raises(WorkbenchContractError):
        SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=True)


def test_reader_receives_exact_review_and_company_once() -> None:
    reader = RecordingCandidateReader(candidate=_candidate())
    submitter = RecordingDecisionSubmitter()

    SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=reader,
        erp_reference_validator=RecordingValidator(),
        decision_submitter=submitter,
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert reader.calls == ({"review_id": "review-1", "company_id": 7},)
    assert len(submitter.commands) == 1


def test_not_ready_or_not_found_returns_result_without_submission() -> None:
    reader = RecordingCandidateReader(error=WorkbenchCandidateNotFoundError("not found"))
    submitter = RecordingDecisionSubmitter()

    result = SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=reader,
        erp_reference_validator=RecordingValidator(),
        decision_submitter=submitter,
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert result.submitted is False
    assert result.review_id == "review-1"
    assert result.status is OdooWorkbenchSubmissionStatus.NOT_READY_OR_NOT_FOUND
    assert result.acknowledgement is None
    assert submitter.commands == ()


def test_none_candidate_return_is_treated_as_not_ready_without_submission() -> None:
    submitter = RecordingDecisionSubmitter()

    result = SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=RecordingCandidateReader(candidate=None),
        erp_reference_validator=RecordingValidator(),
        decision_submitter=submitter,
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert result.status is OdooWorkbenchSubmissionStatus.NOT_READY_OR_NOT_FOUND
    assert submitter.commands == ()


def test_reader_safe_failures_propagate_and_never_submit() -> None:
    error = WorkbenchCandidateReadError("safe read failure")
    submitter = RecordingDecisionSubmitter()

    with pytest.raises(WorkbenchCandidateReadError) as raised:
        SubmitOdooWorkbenchCandidateUseCase(
            candidate_reader=RecordingCandidateReader(error=error),
            erp_reference_validator=RecordingValidator(),
            decision_submitter=submitter,
        ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert raised.value is error
    assert submitter.commands == ()


def test_ambiguous_candidate_failure_never_submits() -> None:
    submitter = RecordingDecisionSubmitter()

    with pytest.raises(WorkbenchCandidateAmbiguityError):
        SubmitOdooWorkbenchCandidateUseCase(
            candidate_reader=RecordingCandidateReader(error=WorkbenchCandidateAmbiguityError("ambiguous")),
            erp_reference_validator=RecordingValidator(),
            decision_submitter=submitter,
        ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert submitter.commands == ()


def test_unexpected_reader_failure_is_wrapped_safely_without_secret_leak() -> None:
    sensitive = RuntimeError("raw provider url token=secret")

    with pytest.raises(WorkbenchSubmissionOrchestrationError) as raised:
        SubmitOdooWorkbenchCandidateUseCase(
            candidate_reader=RecordingCandidateReader(error=sensitive),
            erp_reference_validator=RecordingValidator(),
            decision_submitter=RecordingDecisionSubmitter(),
        ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert str(raised.value) == "Odoo Workbench decision submission orchestration failed."
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is sensitive


def test_candidate_fields_are_mapped_to_review_decision_command_without_rehydrating_allocations() -> None:
    allocation_set = _allocation_set()
    candidate = _candidate(
        line_resolutions=(LineResolution(line_number="1", selected_product_id=800),),
        tax_resolutions=(TaxResolution(line_number="1", tax_index=0, selected_tax_id=900),),
        business_context_allocations=allocation_set,
    )
    submitter = RecordingDecisionSubmitter()

    SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=RecordingCandidateReader(candidate=candidate),
        erp_reference_validator=RecordingValidator(),
        decision_submitter=submitter,
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    command = submitter.commands[0]
    assert command.review_id == candidate.review_id
    assert command.company_id == candidate.company_id
    assert command.expected_version == candidate.expected_version
    assert command.decision is candidate.decision
    assert command.selected_workflow is candidate.selected_workflow
    assert command.selected_partner_id == candidate.selected_partner_id
    assert command.line_resolutions is candidate.line_resolutions
    assert command.tax_resolutions is candidate.tax_resolutions
    assert command.business_context_allocations is allocation_set
    assert command.comment == candidate.comment
    assert command.decided_by == "odoo:11"
    assert command.idempotency_key == candidate.idempotency_key


def test_candidate_company_mismatch_is_rejected_without_override_or_submission() -> None:
    submitter = RecordingDecisionSubmitter()

    with pytest.raises(WorkbenchSubmissionCompanyMismatchError):
        SubmitOdooWorkbenchCandidateUseCase(
            candidate_reader=RecordingCandidateReader(candidate=_candidate(company_id=8)),
            erp_reference_validator=RecordingValidator(),
            decision_submitter=submitter,
        ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert submitter.commands == ()


def test_valid_candidate_submits_once_and_returns_acknowledgement_unchanged() -> None:
    ack = _acknowledgement()
    submitter = RecordingDecisionSubmitter(result=ack)
    validator = RecordingValidator()

    result = SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=RecordingCandidateReader(candidate=_candidate()),
        erp_reference_validator=validator,
        decision_submitter=submitter,
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert result.submitted is True
    assert result.status is OdooWorkbenchSubmissionStatus.SUBMITTED
    assert result.acknowledgement is ack
    assert submitter.commands == (submitter.commands[0],)
    assert validator.calls[0]["requested_company_id"] == 7


def test_validation_failure_prevents_submission() -> None:
    submitter = RecordingDecisionSubmitter()
    validator = RecordingValidator(error=WorkbenchErpReferenceNotFoundError("Sales Order reference is invalid."))

    with pytest.raises(WorkbenchErpReferenceNotFoundError):
        SubmitOdooWorkbenchCandidateUseCase(
            candidate_reader=RecordingCandidateReader(candidate=_candidate()),
            erp_reference_validator=validator,
            decision_submitter=submitter,
        ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert len(validator.calls) == 1
    assert submitter.commands == ()


def test_validation_runs_after_candidate_read_and_before_decision_submission() -> None:
    events: list[str] = []

    SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=RecordingCandidateReader(candidate=_candidate(), events=events),
        erp_reference_validator=RecordingValidator(events=events),
        decision_submitter=RecordingDecisionSubmitter(events=events),
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert events == ["read", "validate", "submit"]


def test_duplicate_candidate_replay_uses_existing_decision_idempotency_once_per_call() -> None:
    ack = _acknowledgement()
    submitter = RecordingDecisionSubmitter(result=ack)
    use_case = SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=RecordingCandidateReader(candidate=_candidate()),
        erp_reference_validator=RecordingValidator(),
        decision_submitter=submitter,
    )

    first = use_case.execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))
    second = use_case.execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert first.acknowledgement is ack
    assert second.acknowledgement is ack
    assert len(submitter.commands) == 2
    assert submitter.commands[0].idempotency_key == "odoo-key-1"
    assert submitter.commands[1].idempotency_key == "odoo-key-1"


def test_stale_version_and_idempotency_conflicts_are_not_retried() -> None:
    for error in (
        ReviewVersionConflictError("stale version"),
        ReviewDecisionIdempotencyConflictError("idempotency conflict"),
    ):
        submitter = RecordingDecisionSubmitter(error=error)
        with pytest.raises(type(error)):
            SubmitOdooWorkbenchCandidateUseCase(
                candidate_reader=RecordingCandidateReader(candidate=_candidate()),
                erp_reference_validator=RecordingValidator(),
                decision_submitter=submitter,
            ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))
        assert len(submitter.commands) == 1


def test_decision_contract_errors_are_not_bypassed_or_retried() -> None:
    submitter = RecordingDecisionSubmitter()

    with pytest.raises(WorkbenchContractError):
        SubmitOdooWorkbenchCandidateUseCase(
            candidate_reader=RecordingCandidateReader(
                candidate=_candidate(decision=ReviewDecisionType.SELECT_WORKFLOW, selected_workflow=None)
            ),
            erp_reference_validator=RecordingValidator(),
            decision_submitter=submitter,
        ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert submitter.commands == ()


def test_valid_dismiss_submits_and_invalid_dismiss_with_allocations_is_rejected_by_command() -> None:
    submitter = RecordingDecisionSubmitter()
    SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=RecordingCandidateReader(
            candidate=_candidate(
                decision=ReviewDecisionType.DISMISS,
                selected_workflow=None,
                selected_partner_id=None,
                business_context_allocations=None,
            )
        ),
        erp_reference_validator=RecordingValidator(),
        decision_submitter=submitter,
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    assert submitter.commands[0].decision is ReviewDecisionType.DISMISS

    with pytest.raises(WorkbenchContractError):
        SubmitOdooWorkbenchCandidateUseCase(
            candidate_reader=RecordingCandidateReader(
                candidate=_candidate(
                    decision=ReviewDecisionType.DISMISS,
                    selected_workflow=None,
                    selected_partner_id=None,
                    business_context_allocations=_allocation_set(),
                )
            ),
            erp_reference_validator=RecordingValidator(),
            decision_submitter=RecordingDecisionSubmitter(),
        ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))


def test_orchestrator_source_has_no_infrastructure_writes_workflows_reference_validation_fuzzy_or_ai() -> None:
    source = Path("app/application/workbench/odoo_submission_use_cases.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "app.erp",
        "app.connectors",
        "app.models",
        "app.persistence",
        "sqlalchemy",
        "fastapi",
        "httpx",
        "json2",
        "search_read",
        ".write(",
        ".create(",
        ".unlink(",
        "acknowledge_decision",
        "decisionengine",
        "workflowstrategy",
        "vendorbillwriter",
        "create_draft",
        "account.move",
        "action_post",
        "customer_invoice",
        "repository",
        "fuzzy",
        "embedding",
        "ai_advisor",
        "ollama",
    )

    for token in forbidden:
        assert token not in source


def test_submission_result_is_immutable_and_validates_status_consistency() -> None:
    result = SubmitOdooWorkbenchCandidateUseCase(
        candidate_reader=RecordingCandidateReader(error=WorkbenchCandidateNotFoundError("not found")),
        erp_reference_validator=RecordingValidator(),
        decision_submitter=RecordingDecisionSubmitter(),
    ).execute(SubmitOdooWorkbenchCandidateCommand(review_id="review-1", company_id=7))

    with pytest.raises(FrozenInstanceError):
        result.submitted = True  # type: ignore[misc]
    with pytest.raises(WorkbenchContractError):
        type(result)(
            submitted=True,
            review_id="review-1",
            status=OdooWorkbenchSubmissionStatus.NOT_READY_OR_NOT_FOUND,
        )


class RecordingCandidateReader:
    def __init__(
        self,
        *,
        candidate: OdooWorkbenchDecisionCandidate | None = None,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.candidate = candidate
        self.error = error
        self.events = events
        self.calls: tuple[dict[str, object], ...] = ()

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate | None:
        self.calls = (*self.calls, {"review_id": review_id, "company_id": company_id})
        if self.events is not None:
            self.events.append("read")
        if self.error is not None:
            raise self.error
        return self.candidate

    def list_ready_decisions(self, *, company_id: int, limit: int) -> tuple[OdooWorkbenchDecisionCandidate, ...]:
        raise AssertionError("orchestrator must read only one exact candidate")


class RecordingDecisionSubmitter:
    def __init__(
        self,
        *,
        result: ReviewDecisionAcknowledgement | None = None,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.events = events
        self.commands: tuple[ReviewDecisionCommand, ...] = ()

    def execute(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        self.commands = (*self.commands, command)
        if self.events is not None:
            self.events.append("submit")
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return _acknowledgement(
            review_id=command.review_id,
            version=command.expected_version + 1,
            decision=command.decision,
            selected_workflow=command.selected_workflow,
        )


class RecordingValidator:
    def __init__(self, *, error: Exception | None = None, events: list[str] | None = None) -> None:
        self.error = error
        self.events = events
        self.calls: tuple[dict[str, object], ...] = ()

    def validate(
        self,
        candidate: OdooWorkbenchDecisionCandidate,
        *,
        requested_company_id: int,
    ) -> OdooWorkbenchDecisionCandidate:
        self.calls = (*self.calls, {"candidate": candidate, "requested_company_id": requested_company_id})
        if self.events is not None:
            self.events.append("validate")
        if self.error is not None:
            raise self.error
        return candidate


def _candidate(**overrides) -> OdooWorkbenchDecisionCandidate:
    values = {
        "odoo_record_id": 42,
        "review_id": "review-1",
        "company_id": 7,
        "expected_version": 4,
        "decision": ReviewDecisionType.SELECT_WORKFLOW,
        "idempotency_key": "odoo-key-1",
        "decided_by_odoo_user_id": 11,
        "decided_at": datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        "decision_ready": True,
        "selected_workflow": WorkflowType.VENDOR_BILL,
        "selected_partner_id": 700,
        "line_resolutions": (),
        "tax_resolutions": (),
        "business_context_allocations": None,
        "comment": "Reviewed in Odoo",
    }
    values.update(overrides)
    return OdooWorkbenchDecisionCandidate(**values)


def _acknowledgement(
    *,
    review_id: str = "review-1",
    version: int = 5,
    decision: ReviewDecisionType = ReviewDecisionType.SELECT_WORKFLOW,
    selected_workflow: WorkflowType | None = WorkflowType.VENDOR_BILL,
) -> ReviewDecisionAcknowledgement:
    return ReviewDecisionAcknowledgement(
        accepted=True,
        review_id=review_id,
        status=ReviewStatus.DECISION_SUBMITTED,
        version=version,
        decision=decision,
        selected_workflow=selected_workflow,
    )


def _allocation_set() -> BusinessContextAllocationSet:
    return BusinessContextAllocationSet(
        allocations=(
            BusinessContextAllocation(
                allocation_key="allocation-1",
                allocation_type=BusinessContextAllocationType.SALES_ORDER_COST,
                amount=Decimal("100.00"),
                currency="TRY",
                sales_order_id=10,
            ),
        ),
        completeness=AllocationCompleteness.COMPLETE,
        invoice_total=Decimal("100.00"),
        currency="TRY",
    )
