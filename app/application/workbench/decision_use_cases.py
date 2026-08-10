from __future__ import annotations

from collections.abc import Callable

from app.application.exceptions import ApplicationError
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import ReviewDecisionAcknowledgement, ReviewDecisionType
from app.application.workbench.exceptions import ReviewDecisionError, WorkbenchContractError
from app.application.workbench.ports import ReviewDecisionWriter, ReviewExecutionEvidenceReader
from app.application.workflow import WorkflowType


class SubmitReviewDecisionUseCase:
    """Application boundary for explicit Workbench review decision submission."""

    def __init__(
        self,
        *,
        review_decision_writer: ReviewDecisionWriter,
        execution_evidence_reader: ReviewExecutionEvidenceReader | None = None,
    ) -> None:
        self._review_decision_writer = review_decision_writer
        self._execution_evidence_reader = execution_evidence_reader

    def execute(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        if not isinstance(command, ReviewDecisionCommand):
            raise WorkbenchContractError("ReviewDecisionCommand is required.")
        if _requires_execution_evidence(command):
            if self._execution_evidence_reader is None:
                raise ReviewDecisionError("Execution source evidence is required for Vendor Bill decisions.")
            evidence = _translate_decision_failure(
                lambda: self._execution_evidence_reader.get_evidence(
                    review_id=command.review_id,
                    company_id=command.company_id,
                    expected_version=command.expected_version,
                ),
                "Execution source evidence could not be loaded safely.",
            )
            return _translate_decision_failure(
                lambda: self._review_decision_writer.submit_review_decision_with_execution_evidence(
                    command,
                    evidence,
                ),
                "Review decision submission failed.",
            )
        return _translate_decision_failure(
            lambda: self._review_decision_writer.submit_review_decision(command),
            "Review decision submission failed.",
        )


def _translate_decision_failure[ResultT](operation: Callable[[], ResultT], fallback_message: str) -> ResultT:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise ReviewDecisionError(fallback_message) from exc


def _requires_execution_evidence(command: ReviewDecisionCommand) -> bool:
    return (
        command.decision is ReviewDecisionType.SELECT_WORKFLOW and command.selected_workflow is WorkflowType.VENDOR_BILL
    )
