from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import OneOffVendorRetirementError
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionStep

SAFE_VENDOR_BILL_EVIDENCE_ERROR = "Vendor Bill execution evidence lookup failed."

_VENDOR_BILL_STEP_TYPE = "vendor_bill"
_COMPLETED_STEP_STATE = "completed"


class SqlAlchemyVendorBillExecutionEvidenceReader:
    """Read-only: does a terminal, successful VENDOR_BILL execution step already
    exist for this review (P0-PROD-08H)?

    Reuses the existing ``workflow_executions``/``workflow_execution_steps``
    durable persistence exactly as-is -- no new tracking is added for this fact.
    A step only reaches ``state='completed'`` after a successful execute with a
    produced Odoo ``account.move`` artifact (see ``execution_runtime_repository``);
    this reader never re-inspects the artifact payload itself.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def has_successful_vendor_bill(self, *, review_id: str, company_id: int) -> bool:
        if not isinstance(review_id, str) or not review_id.strip():
            raise OneOffVendorRetirementError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise OneOffVendorRetirementError("company_id must be positive.")
        try:
            step_id = self._session.scalar(
                select(WorkflowExecutionStep.id)
                .join(WorkflowExecution, WorkflowExecution.execution_id == WorkflowExecutionStep.execution_id)
                .where(
                    WorkflowExecution.review_id == review_id,
                    WorkflowExecution.company_id == company_id,
                    WorkflowExecutionStep.step_type == _VENDOR_BILL_STEP_TYPE,
                    WorkflowExecutionStep.state == _COMPLETED_STEP_STATE,
                )
                .limit(1)
            )
        except SQLAlchemyError as exc:
            raise OneOffVendorRetirementError(SAFE_VENDOR_BILL_EVIDENCE_ERROR) from exc
        return step_id is not None
