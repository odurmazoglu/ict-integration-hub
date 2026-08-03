from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.application.workbench import (
    ReviewDecisionAcknowledgement,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
    WorkbenchContractError,
)
from app.application.workbench.exceptions import ReviewDecisionError, ReviewVersionConflictError
from app.application.workflow import WorkflowType


def test_submit_review_decision_use_case_delegates_exact_command_once() -> None:
    command = _select_workflow_command()
    acknowledgement = ReviewDecisionAcknowledgement(
        accepted=True,
        review_id="review-1",
        status=ReviewStatus.DECISION_SUBMITTED,
        version=2,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
    )
    writer = RecordingDecisionWriter(result=acknowledgement)

    result = SubmitReviewDecisionUseCase(review_decision_writer=writer).execute(command)

    assert result is acknowledgement
    assert writer.commands == (command,)


def test_submit_review_decision_use_case_rejects_non_command_input() -> None:
    use_case = SubmitReviewDecisionUseCase(review_decision_writer=RecordingDecisionWriter())

    with pytest.raises(WorkbenchContractError):
        use_case.execute("not-a-command")  # type: ignore[arg-type]


def test_submit_review_decision_use_case_propagates_known_safe_errors() -> None:
    error = ReviewVersionConflictError("Review item version does not match expected_version.")
    use_case = SubmitReviewDecisionUseCase(review_decision_writer=RecordingDecisionWriter(error=error))

    with pytest.raises(ReviewVersionConflictError) as raised:
        use_case.execute(_select_workflow_command())

    assert raised.value is error


def test_submit_review_decision_use_case_translates_unexpected_errors_safely() -> None:
    sensitive = RuntimeError("sql password=secret token=abc")
    use_case = SubmitReviewDecisionUseCase(review_decision_writer=RecordingDecisionWriter(error=sensitive))

    with pytest.raises(ReviewDecisionError) as raised:
        use_case.execute(_select_workflow_command())

    assert str(raised.value) == "Review decision submission failed."
    assert "secret" not in str(raised.value)
    assert "token" not in str(raised.value)
    assert raised.value.__cause__ is sensitive


def test_submit_review_decision_acknowledgement_is_immutable() -> None:
    acknowledgement = ReviewDecisionAcknowledgement(
        accepted=True,
        review_id="review-1",
        status=ReviewStatus.DECISION_SUBMITTED,
        version=2,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
    )

    with pytest.raises(FrozenInstanceError):
        acknowledgement.version = 3


def test_submit_review_decision_use_case_does_not_import_infrastructure_or_provider_boundaries() -> None:
    source = Path("app/application/workbench/decision_use_cases.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "sqlalchemy",
        "app.models",
        "app.db",
        "app.persistence",
        "app.connectors",
        "app.erp",
        "odoo",
        "uyumsoft",
        "fastapi",
        "httpx",
        "soap",
        "zeep",
    )

    for token in forbidden:
        assert token not in source


def test_submit_review_decision_use_case_does_not_execute_workflows_or_erp_writes() -> None:
    source = Path("app/application/workbench/decision_use_cases.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "decisionengine",
        "workflowstrategy",
        "vendorbillwriter",
        "manualreviewstrategy",
        "account.move",
        "action_post",
        "create_draft",
        "commit",
        "rollback",
        "flush",
        "ai_advisor",
        "ollama",
        "fuzzy",
        "embedding",
    )

    for token in forbidden:
        assert token not in source


class RecordingDecisionWriter:
    def __init__(
        self,
        *,
        result: ReviewDecisionAcknowledgement | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.commands: tuple[ReviewDecisionCommand, ...] = ()

    def submit_review_decision(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        self.commands = (*self.commands, command)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return ReviewDecisionAcknowledgement(
            accepted=True,
            review_id=command.review_id,
            status=ReviewStatus.DECISION_SUBMITTED,
            version=command.expected_version + 1,
            decision=command.decision,
            selected_workflow=command.selected_workflow,
        )


def _select_workflow_command() -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id="review-1",
        company_id=7,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        decided_by="finance.user",
        idempotency_key="decision-key-1",
    )
