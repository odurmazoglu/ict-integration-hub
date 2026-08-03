from __future__ import annotations

from collections.abc import Callable

from app.application.exceptions import ApplicationError
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import ReviewDecisionAcknowledgement
from app.application.workbench.exceptions import ReviewDecisionError, WorkbenchContractError
from app.application.workbench.ports import ReviewDecisionWriter


class SubmitReviewDecisionUseCase:
    """Application boundary for explicit Workbench review decision submission."""

    def __init__(self, *, review_decision_writer: ReviewDecisionWriter) -> None:
        self._review_decision_writer = review_decision_writer

    def execute(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        if not isinstance(command, ReviewDecisionCommand):
            raise WorkbenchContractError("ReviewDecisionCommand is required.")
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
