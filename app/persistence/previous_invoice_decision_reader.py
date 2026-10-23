from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.execution.contracts import ExecutionMode
from app.application.execution.runtime import ExecutionState
from app.application.workbench.accounting_resolution import ReviewAccountingResolution
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import ReviewPersistenceError, WorkbenchContractError
from app.application.workbench.previous_invoice_guidance import (
    PREVIOUS_INVOICE_SCAN_LIMIT,
    PreviousInvoiceDecision,
)
from app.application.workbench.purchase_purpose import PurchasePurposeResolution
from app.application.workflow import WorkflowType
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.models.workflow_execution import WorkflowExecution
from app.persistence.workbench_review_accounting_resolution_repository import (
    _resolution_from_model as _accounting_from_model,
)
from app.persistence.workbench_review_purchase_purpose_resolution_repository import (
    _resolution_from_model as _purpose_from_model,
)

SAFE_PREVIOUS_INVOICE_READ_ERROR = "Previous invoice decisions could not be read."


class SqlAlchemyPreviousInvoiceDecisionReader:
    """Committed, read-only view of a supplier's completed previous invoices (V1 guidance).

    "Completed" means an accepted Direct Vendor Bill decision whose live (``execute``)
    execution completed, i.e. the draft bill exists. For each such review the purpose and
    accounting resolution are the latest ones recorded *up to* the decided version, so a
    resolution of a superseded version is used only if nothing newer was recorded before
    the decision. A review missing either is skipped. Reads Hub rows only; never Odoo.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_completed_supplier_decisions(
        self,
        *,
        company_id: int,
        supplier_tax_number: str,
        exclude_review_id: str,
        limit: int = PREVIOUS_INVOICE_SCAN_LIMIT,
    ) -> tuple[PreviousInvoiceDecision, ...]:
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        if not isinstance(supplier_tax_number, str) or not supplier_tax_number.strip():
            raise WorkbenchContractError("supplier_tax_number is required.")
        if not isinstance(exclude_review_id, str) or not exclude_review_id.strip():
            raise WorkbenchContractError("exclude_review_id is required.")
        if type(limit) is not int or limit <= 0:
            raise WorkbenchContractError("limit must be positive.")
        try:
            rows = self._completed_decisions(company_id, supplier_tax_number.strip(), exclude_review_id, limit)
            decisions = []
            for item, decision in rows:
                purpose = self._latest_purpose(item.review_id, company_id, decision.review_version_before)
                accounting = self._latest_accounting(item.review_id, company_id, decision.review_version_before)
                if purpose is None or accounting is None:
                    continue
                decisions.append(
                    PreviousInvoiceDecision(
                        review_id=item.review_id,
                        invoice_number=item.invoice_number,
                        invoice_date=item.invoice_date,
                        supplier_name=item.supplier_name,
                        decided_at=decision.submitted_at,
                        purchase_purpose=purpose.purchase_purpose,
                        accounting_resolution=accounting,
                    )
                )
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PREVIOUS_INVOICE_READ_ERROR) from exc
        return tuple(decisions)

    def _completed_decisions(
        self, company_id: int, supplier_tax_number: str, exclude_review_id: str, limit: int
    ) -> list[tuple[WorkbenchReviewItem, WorkbenchReviewDecision]]:
        statement = (
            select(WorkbenchReviewItem, WorkbenchReviewDecision)
            .join(
                WorkbenchReviewDecision,
                and_(
                    WorkbenchReviewDecision.review_id == WorkbenchReviewItem.review_id,
                    WorkbenchReviewDecision.company_id == WorkbenchReviewItem.company_id,
                ),
            )
            .where(
                WorkbenchReviewItem.company_id == company_id,
                WorkbenchReviewItem.supplier_tax_number == supplier_tax_number,
                WorkbenchReviewItem.review_id != exclude_review_id,
                WorkbenchReviewDecision.decision_type == ReviewDecisionType.SELECT_WORKFLOW.value,
                WorkbenchReviewDecision.selected_workflow == WorkflowType.VENDOR_BILL.value,
                select(WorkflowExecution.id)
                .where(
                    WorkflowExecution.review_id == WorkbenchReviewItem.review_id,
                    WorkflowExecution.company_id == WorkbenchReviewItem.company_id,
                    WorkflowExecution.decision_version == WorkbenchReviewDecision.review_version_after,
                    WorkflowExecution.mode == ExecutionMode.EXECUTE.value,
                    WorkflowExecution.state == ExecutionState.COMPLETED.value,
                )
                .exists(),
            )
            .order_by(
                WorkbenchReviewItem.invoice_date.desc().nulls_last(),
                WorkbenchReviewDecision.submitted_at.desc(),
                WorkbenchReviewItem.review_id,
            )
            .limit(limit)
        )
        return [(item, decision) for item, decision in self._session.execute(statement).all()]

    def _latest_purpose(self, review_id: str, company_id: int, max_version: int) -> PurchasePurposeResolution | None:
        record = self._session.scalars(
            select(WorkbenchReviewPurchasePurposeResolution)
            .where(
                WorkbenchReviewPurchasePurposeResolution.review_id == review_id,
                WorkbenchReviewPurchasePurposeResolution.company_id == company_id,
                WorkbenchReviewPurchasePurposeResolution.review_version <= max_version,
            )
            .order_by(WorkbenchReviewPurchasePurposeResolution.review_version.desc())
            .limit(1)
        ).first()
        return _purpose_from_model(record) if record is not None else None

    def _latest_accounting(
        self, review_id: str, company_id: int, max_version: int
    ) -> ReviewAccountingResolution | None:
        record = self._session.scalars(
            select(WorkbenchReviewAccountingResolution)
            .where(
                WorkbenchReviewAccountingResolution.review_id == review_id,
                WorkbenchReviewAccountingResolution.company_id == company_id,
                WorkbenchReviewAccountingResolution.review_version <= max_version,
            )
            .order_by(WorkbenchReviewAccountingResolution.review_version.desc())
            .limit(1)
        ).first()
        return _accounting_from_model(record) if record is not None else None


__all__ = ["SAFE_PREVIOUS_INVOICE_READ_ERROR", "SqlAlchemyPreviousInvoiceDecisionReader"]
