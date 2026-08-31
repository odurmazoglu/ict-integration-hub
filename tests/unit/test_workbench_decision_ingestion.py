from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    OdooWorkbenchDecisionCandidate,
    ReviewDecisionAcknowledgement,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewStatus,
    WorkbenchCandidateReadError,
    WorkbenchCandidateUnsupportedDecisionError,
    WorkbenchContractError,
    WorkbenchDecisionIngestionCandidateResult,
    WorkbenchDecisionIngestionStatus,
    WorkbenchDecisionIngestionWorkflow,
    WorkbenchErpReferenceNotFoundError,
    WorkbenchProjectionPublishError,
    decision_idempotency_key,
)
from app.application.workbench.exceptions import (
    ReviewDecisionIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewVersionConflictError,
)
from app.application.workflow import WorkflowType


def test_ingestion_result_is_immutable_and_validates_status() -> None:
    result = WorkbenchDecisionIngestionCandidateResult(
        review_id="review-1",
        odoo_record_id=42,
        status=WorkbenchDecisionIngestionStatus.PROCESSED,
    )

    with pytest.raises(FrozenInstanceError):
        result.acknowledged = True  # type: ignore[misc]
    with pytest.raises(WorkbenchContractError):
        WorkbenchDecisionIngestionCandidateResult(review_id="", odoo_record_id=42, status="processed")  # type: ignore[arg-type]


def test_ready_candidates_are_read_by_company_and_limit_and_submitted_to_canonical_path() -> None:
    candidate = _candidate()
    reader = RecordingCandidateReader([candidate])
    submitter = RecordingDecisionSubmitter()

    result = _workflow(reader=reader, submitter=submitter).sync_ready_decisions(
        company_id=7,
        limit=10,
        trace_id="trace-1",
    )

    assert reader.calls == [{"company_id": 7, "limit": 10}]
    assert result.processed_count == 1
    assert len(submitter.commands) == 1
    command = submitter.commands[0]
    assert isinstance(command, ReviewDecisionCommand)
    assert command.review_id == "review-1"
    assert command.company_id == 7
    assert command.expected_version == 4
    assert command.decision is ReviewDecisionType.SELECT_WORKFLOW
    assert command.idempotency_key == decision_idempotency_key(candidate)
    assert command.idempotency_key != "user-edited-odoo-key"


def test_hub_decision_persists_before_odoo_acknowledgement() -> None:
    events: list[str] = []

    result = _workflow(
        reader=RecordingCandidateReader([_candidate()]),
        validator=RecordingValidator(events=events),
        submitter=RecordingDecisionSubmitter(events=events),
        publisher=RecordingAcknowledgementPublisher(events=events),
        unit_of_work=RecordingUnitOfWork(events=events),
    ).sync_ready_decisions(company_id=7, trace_id="trace-1")

    assert result.results[0].status is WorkbenchDecisionIngestionStatus.PROCESSED
    assert events == ["validate", "submit", "commit", "ack"]


def test_acknowledgement_success_sets_submitted_lifecycle_without_resolving_review() -> None:
    publisher = RecordingAcknowledgementPublisher()

    result = _workflow(
        reader=RecordingCandidateReader([_candidate()]),
        publisher=publisher,
    ).sync_ready_decisions(company_id=7, trace_id="trace-1")

    assert result.results[0].acknowledged is True
    assert publisher.calls[0]["acknowledgement"].status is ReviewStatus.DECISION_SUBMITTED
    assert publisher.calls[0]["acknowledgement"].status is not ReviewStatus.RESOLVED
    assert publisher.calls[0]["clear_ready"] is True
    assert publisher.calls[0]["idempotency_key"] == result.results[0].idempotency_key


def test_acknowledgement_failure_leaves_hub_commit_and_returns_replayable_failure() -> None:
    unit_of_work = RecordingUnitOfWork()
    publisher = RecordingAcknowledgementPublisher(error=WorkbenchProjectionPublishError("Odoo ack failed."))

    result = _workflow(
        reader=RecordingCandidateReader([_candidate()]),
        publisher=publisher,
        unit_of_work=unit_of_work,
    ).sync_ready_decisions(company_id=7, trace_id="trace-1")

    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0
    assert result.results[0].status is WorkbenchDecisionIngestionStatus.ACKNOWLEDGEMENT_FAILED
    assert result.results[0].acknowledged is False


def test_replay_after_ack_failure_uses_existing_decision_and_retries_ack_without_duplicate_submit() -> None:
    candidate = _candidate()
    submitter = RecordingDecisionSubmitter()
    failing_publisher = RecordingAcknowledgementPublisher(error=WorkbenchProjectionPublishError("Odoo ack failed."))

    first = _workflow(
        reader=RecordingCandidateReader([candidate]),
        submitter=submitter,
        publisher=failing_publisher,
    ).sync_ready_decisions(company_id=7)

    retry_publisher = RecordingAcknowledgementPublisher()
    second = _workflow(
        reader=RecordingCandidateReader([candidate]),
        submitter=submitter,
        publisher=retry_publisher,
    ).sync_ready_decisions(company_id=7)

    assert first.results[0].status is WorkbenchDecisionIngestionStatus.ACKNOWLEDGEMENT_FAILED
    assert second.results[0].status is WorkbenchDecisionIngestionStatus.ALREADY_PROCESSED
    assert len(submitter.persisted_commands) == 1
    assert len(submitter.commands) == 2
    assert len(retry_publisher.calls) == 1


def test_same_decision_replay_returns_already_processed_with_same_idempotency_key() -> None:
    candidate = _candidate()
    submitter = RecordingDecisionSubmitter()
    workflow = _workflow(reader=RecordingCandidateReader([candidate]), submitter=submitter)

    first = workflow.sync_ready_decisions(company_id=7)
    second = workflow.sync_ready_decisions(company_id=7)

    assert first.results[0].status is WorkbenchDecisionIngestionStatus.PROCESSED
    assert second.results[0].status is WorkbenchDecisionIngestionStatus.ALREADY_PROCESSED
    assert first.results[0].idempotency_key == second.results[0].idempotency_key
    assert len(submitter.persisted_commands) == 1


def test_conflicting_different_payload_for_same_review_version_fails_closed() -> None:
    fixed_key = decision_idempotency_key(_candidate(comment="first"))
    submitter = RecordingDecisionSubmitter(existing_by_key={fixed_key: _candidate(comment="first")})
    conflicting = _candidate(comment="second")

    result = _workflow(
        reader=RecordingCandidateReader([conflicting]),
        submitter=submitter,
        idempotency_key_override=fixed_key,
    ).sync_ready_decisions(company_id=7)

    assert result.results[0].status is WorkbenchDecisionIngestionStatus.CONFLICT


def test_validation_failures_are_isolated_per_candidate() -> None:
    valid = _candidate(review_id="review-1", odoo_record_id=42)
    invalid = OdooWorkbenchDecisionCandidateReadFailureFactory.unsupported(review_id="review-2", odoo_record_id=43)
    submitter = RecordingDecisionSubmitter()

    result = _workflow(reader=RecordingCandidateReader([invalid, valid]), submitter=submitter).sync_ready_decisions(
        company_id=7
    )

    assert [item.status for item in result.results] == [
        WorkbenchDecisionIngestionStatus.UNSUPPORTED_DECISION,
        WorkbenchDecisionIngestionStatus.PROCESSED,
    ]
    assert len(submitter.commands) == 1


def test_expected_read_and_validation_failures_are_safe_results() -> None:
    read_result = _workflow(
        reader=RecordingCandidateReader(error=WorkbenchCandidateReadError("Odoo read failed."))
    ).sync_ready_decisions(company_id=7)
    missing_reference = _workflow(
        reader=RecordingCandidateReader([_candidate()]),
        validator=RecordingValidator(error=WorkbenchErpReferenceNotFoundError("ERP reference invalid.")),
    ).sync_ready_decisions(company_id=7)

    assert read_result.results[0].status is WorkbenchDecisionIngestionStatus.READ_FAILED
    assert missing_reference.results[0].status is WorkbenchDecisionIngestionStatus.INVALID_ALLOCATION


def test_missing_review_company_mismatch_and_stale_version_fail_closed() -> None:
    cases = [
        (ReviewNotFoundError("Review item was not found."), WorkbenchDecisionIngestionStatus.REVIEW_NOT_FOUND),
        (
            ReviewVersionConflictError("Review item version does not match expected_version."),
            WorkbenchDecisionIngestionStatus.STALE_REVIEW_VERSION,
        ),
    ]
    for error, status in cases:
        result = _workflow(
            reader=RecordingCandidateReader([_candidate()]),
            submitter=RecordingDecisionSubmitter(error=error),
        ).sync_ready_decisions(company_id=7)
        assert result.results[0].status is status

    company_result = _workflow(reader=RecordingCandidateReader([_candidate(company_id=8)])).sync_ready_decisions(
        company_id=7
    )
    assert company_result.results[0].status is WorkbenchDecisionIngestionStatus.COMPANY_MISMATCH


def test_unexpected_programming_errors_are_not_swallowed() -> None:
    with pytest.raises(TypeError):
        _workflow(
            reader=RecordingCandidateReader([_candidate()]),
            submitter=RecordingDecisionSubmitter(error=TypeError("programming defect")),
        ).sync_ready_decisions(company_id=7)


def test_architecture_guards_no_execution_uyumsoft_ai_fuzzy_or_duplicate_decision_contracts() -> None:
    source = Path("app/application/workbench/decision_ingestion.py").read_text(encoding="utf-8").lower()

    forbidden = (
        "app.erp",
        "app.connectors",
        "app.models",
        "sqlalchemy",
        "executionruntime",
        "vendorbill",
        "account.move",
        "purchase.order",
        "uyumsoft",
        "fuzzy",
        "levenshtein",
        "embedding",
        "ai_advisor",
        "request_investigation",
    )
    for token in forbidden:
        assert token not in source


class RecordingCandidateReader:
    def __init__(self, results: list[object] | None = None, *, error: Exception | None = None) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[dict[str, int]] = []

    def list_ready_decision_results(self, *, company_id: int, limit: int) -> tuple[object, ...]:
        self.calls.append({"company_id": company_id, "limit": limit})
        if self.error is not None:
            raise self.error
        return tuple(self.results)

    def list_ready_decisions(self, *, company_id: int, limit: int) -> tuple[OdooWorkbenchDecisionCandidate, ...]:
        del company_id, limit
        return tuple(result for result in self.results if isinstance(result, OdooWorkbenchDecisionCandidate))

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate:
        raise AssertionError("runtime scan must not call single-candidate lookup")


class RecordingValidator:
    def __init__(self, *, events: list[str] | None = None, error: Exception | None = None) -> None:
        self.events = events
        self.error = error

    def validate(
        self,
        candidate: OdooWorkbenchDecisionCandidate,
        *,
        requested_company_id: int,
    ) -> OdooWorkbenchDecisionCandidate:
        assert candidate.company_id == requested_company_id
        if self.events is not None:
            self.events.append("validate")
        if self.error is not None:
            raise self.error
        return candidate


class RecordingDecisionSubmitter:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        error: Exception | None = None,
        existing_by_key: dict[str, OdooWorkbenchDecisionCandidate] | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.commands: list[ReviewDecisionCommand] = []
        self.persisted_commands: list[ReviewDecisionCommand] = []
        self.existing_by_key = existing_by_key or {}

    def execute(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        self.commands.append(command)
        if self.events is not None:
            self.events.append("submit")
        if self.error is not None:
            raise self.error
        existing = self.existing_by_key.get(command.idempotency_key)
        if existing is not None:
            if decision_idempotency_key(existing) != command.idempotency_key:
                raise AssertionError("test setup supplied inconsistent existing decision key")
            existing_command = _command_from_candidate(existing, command.idempotency_key)
            if _command_fingerprint(command) != _command_fingerprint(existing_command):
                raise ReviewDecisionIdempotencyConflictError("Review decision idempotency key conflicts.")
            return _ack(command, version=command.expected_version + 1)
        self.existing_by_key[command.idempotency_key] = _candidate_from_command(command)
        self.persisted_commands.append(command)
        return _ack(command, version=command.expected_version + 1)

    def has_matching_decision(self, command: ReviewDecisionCommand) -> bool:
        existing = self.existing_by_key.get(command.idempotency_key)
        if existing is None:
            return False
        existing_command = _command_from_candidate(existing, command.idempotency_key)
        return _command_fingerprint(command) == _command_fingerprint(existing_command)


class RecordingAcknowledgementPublisher:
    def __init__(self, *, events: list[str] | None = None, error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def acknowledge_decision(
        self,
        acknowledgement: ReviewDecisionAcknowledgement,
        *,
        odoo_record_id: int,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        clear_ready: bool = False,
    ) -> object:
        if self.events is not None:
            self.events.append("ack")
        self.calls.append(
            {
                "acknowledgement": acknowledgement,
                "odoo_record_id": odoo_record_id,
                "trace_id": trace_id,
                "idempotency_key": idempotency_key,
                "clear_ready": clear_ready,
            }
        )
        if self.error is not None:
            raise self.error
        return object()


class RecordingUnitOfWork:
    def __init__(self, *, events: list[str] | None = None) -> None:
        self.events = events
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1
        if self.events is not None:
            self.events.append("commit")

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.events is not None:
            self.events.append("rollback")


class OdooWorkbenchDecisionCandidateReadFailureFactory:
    @staticmethod
    def unsupported(*, review_id: str, odoo_record_id: int) -> object:
        from app.application.workbench.projection import OdooWorkbenchDecisionCandidateReadFailure

        return OdooWorkbenchDecisionCandidateReadFailure(
            review_id=review_id,
            odoo_record_id=odoo_record_id,
            error=WorkbenchCandidateUnsupportedDecisionError("Unsupported Odoo decision."),
        )


def _workflow(
    *,
    reader: RecordingCandidateReader,
    validator: RecordingValidator | None = None,
    submitter: RecordingDecisionSubmitter | None = None,
    publisher: RecordingAcknowledgementPublisher | None = None,
    unit_of_work: RecordingUnitOfWork | None = None,
    idempotency_key_override: str | None = None,
) -> WorkbenchDecisionIngestionWorkflow:
    workflow = WorkbenchDecisionIngestionWorkflow(
        candidate_reader=reader,
        erp_reference_validator=validator or RecordingValidator(),
        decision_submitter=submitter or RecordingDecisionSubmitter(),
        acknowledgement_publisher=publisher or RecordingAcknowledgementPublisher(),
        unit_of_work=unit_of_work or RecordingUnitOfWork(),
        idempotency_key_factory=(lambda candidate: idempotency_key_override or decision_idempotency_key(candidate)),
    )
    return workflow


def _candidate(
    *,
    review_id: str = "review-1",
    odoo_record_id: int = 42,
    company_id: int = 7,
    comment: str | None = "Reviewed in Odoo.",
) -> OdooWorkbenchDecisionCandidate:
    return OdooWorkbenchDecisionCandidate(
        odoo_record_id=odoo_record_id,
        review_id=review_id,
        company_id=company_id,
        expected_version=4,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        idempotency_key="user-edited-odoo-key",
        decided_by_odoo_user_id=11,
        decided_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        decision_ready=True,
        selected_workflow=WorkflowType.EXPENSE,
        selected_partner_id=700,
        line_resolutions=(),
        tax_resolutions=(),
        business_context_allocations=_allocation_set(),
        comment=comment,
    )


def _allocation_set() -> BusinessContextAllocationSet:
    return BusinessContextAllocationSet(
        allocations=(
            BusinessContextAllocation(
                allocation_key="ALLOC-001",
                allocation_type=BusinessContextAllocationType.OPERATING_EXPENSE,
                amount=Decimal("100.00"),
                currency="TRY",
            ),
        ),
        completeness=AllocationCompleteness.COMPLETE,
        invoice_total=Decimal("100.00"),
        currency="TRY",
    )


def _command_from_candidate(candidate: OdooWorkbenchDecisionCandidate, idempotency_key: str) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=candidate.review_id,
        company_id=candidate.company_id,
        expected_version=candidate.expected_version,
        decision=candidate.decision,
        decided_by=f"odoo:{candidate.decided_by_odoo_user_id}",
        idempotency_key=idempotency_key,
        selected_workflow=candidate.selected_workflow,
        selected_partner_id=candidate.selected_partner_id,
        line_resolutions=candidate.line_resolutions,
        tax_resolutions=candidate.tax_resolutions,
        business_context_allocations=candidate.business_context_allocations,
        comment=candidate.comment,
    )


def _candidate_from_command(command: ReviewDecisionCommand) -> OdooWorkbenchDecisionCandidate:
    return OdooWorkbenchDecisionCandidate(
        odoo_record_id=42,
        review_id=command.review_id,
        company_id=command.company_id,
        expected_version=command.expected_version,
        decision=command.decision,
        idempotency_key="ignored",
        decided_by_odoo_user_id=11,
        decided_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        decision_ready=True,
        selected_workflow=command.selected_workflow,
        selected_partner_id=command.selected_partner_id,
        line_resolutions=command.line_resolutions,
        tax_resolutions=command.tax_resolutions,
        business_context_allocations=command.business_context_allocations,
        comment=command.comment,
    )


def _command_fingerprint(command: ReviewDecisionCommand) -> tuple[object, ...]:
    return (
        command.company_id,
        command.review_id,
        command.expected_version,
        command.decision,
        command.selected_workflow,
        command.selected_partner_id,
        command.business_context_allocations,
        command.comment,
        command.decided_by,
    )


def _ack(
    command: ReviewDecisionCommand,
    *,
    version: int,
    warnings: tuple[str, ...] = (),
) -> ReviewDecisionAcknowledgement:
    return ReviewDecisionAcknowledgement(
        accepted=True,
        review_id=command.review_id,
        status=ReviewStatus.DECISION_SUBMITTED,
        version=version,
        decision=command.decision,
        selected_workflow=command.selected_workflow,
        warnings=warnings,
    )
