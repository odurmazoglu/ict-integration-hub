"""One thin handler per operator request action (ADR-0013).

Each handler builds the *existing* command of one *existing* use case from the typed
request and invokes it unchanged. Handlers translate the use case's own result into a
short operator message; they never decide eligibility, never pick accounts, never
retry against a newer version. Exceptions raised by the use cases propagate to
:class:`OperatorRequestIngestionWorkflow`, which classifies them (stale / rejected /
retry later).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

from app.application.execution import (
    ExecutionApproval,
    ExecutionArtifactType,
    ExecutionMode,
    WorkbenchVendorBillExecutionResult,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.workbench.accounting_resolution import (
    AccountingTreatmentType,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_ingestion import decision_idempotency_key, review_decision_command
from app.application.workbench.dto import ReviewDecisionAcknowledgement
from app.application.workbench.exceptions import ReviewVersionConflictError, WorkbenchContractError
from app.application.workbench.operator_request_ingestion import (
    ALREADY_COMPLETED_MESSAGE,
    STALE_REQUEST_MESSAGE,
    WRITING_SUPPLIER_MODES,
    OperatorActionContext,
    OperatorActionOutcome,
    OperatorRequest,
    OperatorRequestOutcome,
)
from app.application.workbench.projection import OdooWorkbenchDecisionCandidate
from app.application.workbench.purchase_purpose import SubmitPurchasePurposeCommand
from app.application.workbench.supplier_remediation import ResolveWorkbenchSupplierCommand
from app.application.workbench.supplier_resolution import SupplierResolutionMode

#: Existing ``WriteAuthorizationOperationType`` values, by supplier mode that writes a partner.
SUPPLIER_AUTHORIZATION_OPERATION = {
    SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER: "CREATE_PERMANENT_SUPPLIER",
    SupplierResolutionMode.ONE_OFF_VENDOR: "ONE_OFF_VENDOR_SUPPLIER",
}
EXECUTE_VENDOR_BILL_OPERATION = "EXECUTE_VENDOR_BILL"


def run_coroutine[T](awaitable: Awaitable[T]) -> T:
    """The poller tick is synchronous; async use cases run to completion here."""

    async def _await() -> T:
        return await awaitable

    return asyncio.run(_await())


Runner = Callable[[Coroutine[Any, Any, Any] | Awaitable[Any]], Any]


class _AsyncUseCase(Protocol):
    def execute(self, command: Any) -> Awaitable[Any]: ...


class _SyncUseCase(Protocol):
    def execute(self, command: Any) -> Any: ...


class SupplierResolutionRequestHandler:
    """-> ``ResolveWorkbenchSupplierUseCase`` (POST /reviews/{id}/supplier-resolution)."""

    def __init__(self, *, use_case: _AsyncUseCase, runner: Runner = run_coroutine) -> None:
        self._use_case = use_case
        self._runner = runner

    def handle(self, request: OperatorRequest, context: OperatorActionContext) -> OperatorActionOutcome:
        mode = request.supplier_mode
        if mode is None:
            raise WorkbenchContractError("Tedarikçi işlemi seçilmelidir.")
        authorization_id = None
        if mode in WRITING_SUPPLIER_MODES:
            # Same narrow single-use authorization an operator would issue through the API;
            # the use case still enforces the kill switch and its own eligibility.
            authorization_id = context.ensure_authorization(SUPPLIER_AUTHORIZATION_OPERATION[mode])
        result = self._runner(
            self._use_case.execute(
                ResolveWorkbenchSupplierCommand(
                    review_id=request.review_id,
                    company_id=request.company_id,
                    expected_version=request.expected_version,
                    mode=mode,
                    approved_by=context.actor.actor,
                    resolved_partner_id=request.partner_id,
                    note=request.note,
                    authorization_id=authorization_id,
                )
            )
        )
        return _applied(result, "Tedarikçi çözümü kaydedildi.")


class PurchasePurposeRequestHandler:
    """-> ``SubmitPurchasePurposeUseCase`` (POST /reviews/{id}/purchase-purpose)."""

    def __init__(self, *, use_case: _SyncUseCase) -> None:
        self._use_case = use_case

    def handle(self, request: OperatorRequest, context: OperatorActionContext) -> OperatorActionOutcome:
        if request.purchase_purpose is None:
            raise WorkbenchContractError("Satın alma amacı seçilmelidir.")
        result = self._use_case.execute(
            SubmitPurchasePurposeCommand(
                review_id=request.review_id,
                company_id=request.company_id,
                expected_version=request.expected_version,
                purchase_purpose=request.purchase_purpose,
                approved_by=context.actor.actor,
                note=request.note,
            )
        )
        return _applied(result, "Satın alma amacı kaydedildi.")


class AccountingResolutionRequestHandler:
    """-> ``SubmitReviewAccountingResolutionUseCase`` (POST /reviews/{id}/accounting-resolution)."""

    def __init__(self, *, use_case: _AsyncUseCase, runner: Runner = run_coroutine) -> None:
        self._use_case = use_case
        self._runner = runner

    def handle(self, request: OperatorRequest, context: OperatorActionContext) -> OperatorActionOutcome:
        treatment = request.treatment_type
        if treatment is None:
            raise WorkbenchContractError("Muhasebe işlemi seçilmelidir.")
        # Only the selected treatment's own inputs are forwarded; the other treatment's
        # Odoo fields may still hold an earlier choice and must not leak into the command.
        expense = treatment is AccountingTreatmentType.EXPENSE_ACCOUNT
        result = self._runner(
            self._use_case.execute(
                SubmitReviewAccountingResolutionCommand(
                    review_id=request.review_id,
                    company_id=request.company_id,
                    expected_version=request.expected_version,
                    treatment_type=treatment,
                    approved_by=context.actor.actor,
                    expense_account_id=request.expense_account_id if expense else None,
                    expense_category=request.expense_category if expense else None,
                    asset_account_id=None if expense else request.asset_account_id,
                    depreciation_model_id=None if expense else request.depreciation_model_id,
                    note=request.note,
                )
            )
        )
        return _applied(result, "Muhasebe işlemi kaydedildi.")


class _DecisionCandidateReader(Protocol):
    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate: ...


class _ErpReferenceValidator(Protocol):
    def validate(self, candidate: OdooWorkbenchDecisionCandidate, *, requested_company_id: int) -> object: ...


class _DecisionSubmitter(Protocol):
    def execute(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement: ...


class _UnitOfWork(Protocol):
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class DecisionRequestHandler:
    """-> existing decision candidate parsing + ``SubmitReviewDecisionUseCase``.

    The decision inputs are the existing Workbench decision fields and allocation child
    rows (ADR-0011/0012). The reader is configured so its expected-version / ready /
    decided-by / decided-at fields are the request's snapshotted fields, which keeps the
    decision tied to the version the operator saw.
    """

    def __init__(
        self,
        *,
        candidate_reader: _DecisionCandidateReader,
        erp_reference_validator: _ErpReferenceValidator,
        decision_submitter: _DecisionSubmitter,
        unit_of_work: _UnitOfWork,
    ) -> None:
        self._reader = candidate_reader
        self._validator = erp_reference_validator
        self._submitter = decision_submitter
        self._unit_of_work = unit_of_work

    def handle(self, request: OperatorRequest, context: OperatorActionContext) -> OperatorActionOutcome:
        candidate = self._reader.get_ready_decision(review_id=request.review_id, company_id=request.company_id)
        if candidate.odoo_record_id != request.odoo_record_id or candidate.expected_version != request.expected_version:
            # The row changed between the request scan and the decision read.
            raise ReviewVersionConflictError(STALE_REQUEST_MESSAGE)
        self._validator.validate(candidate, requested_company_id=request.company_id)
        command = review_decision_command(candidate, idempotency_key=decision_idempotency_key(candidate))
        already = bool(getattr(self._submitter, "has_matching_decision", lambda _command: False)(command))
        try:
            acknowledgement = self._submitter.execute(command)
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
        if already:
            return OperatorActionOutcome(
                outcome=OperatorRequestOutcome.ALREADY_COMPLETED, message=ALREADY_COMPLETED_MESSAGE
            )
        return OperatorActionOutcome(
            outcome=OperatorRequestOutcome.COMPLETED,
            message=f"Karar kaydedildi (inceleme v{acknowledgement.version}).",
        )


class _ExecutionDispatcher(Protocol):
    def execute(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        mode: ExecutionMode,
        approval: ExecutionApproval | None,
        trace_id: str | None,
        authorization_id: str | None,
    ) -> WorkbenchVendorBillExecutionResult: ...


class ExecuteVendorBillRequestHandler:
    """-> existing write authorization + ``WorkbenchAcceptedDecisionExecutionDispatcher``.

    Mirrors the documented production run: issue one narrow ``EXECUTE_VENDOR_BILL``
    authorization for exactly this review/version, then execute in EXECUTE mode with the
    actor as named approver. The bill is created as a draft; posting stays a human Odoo
    action.
    """

    def __init__(self, *, dispatcher: _ExecutionDispatcher) -> None:
        self._dispatcher = dispatcher

    def handle(self, request: OperatorRequest, context: OperatorActionContext) -> OperatorActionOutcome:
        authorization_id = context.ensure_authorization(EXECUTE_VENDOR_BILL_OPERATION)
        result = self._dispatcher.execute(
            review_id=request.review_id,
            company_id=request.company_id,
            decision_version=request.expected_version,
            mode=ExecutionMode.EXECUTE,
            approval=ExecutionApproval(approved_by=context.actor.actor),
            trace_id=context.trace_id,
            authorization_id=authorization_id,
        )
        return _execution_outcome(result)


def _applied(result: object, message: str) -> OperatorActionOutcome:
    if getattr(result, "already_applied", False) is True:
        return OperatorActionOutcome(
            outcome=OperatorRequestOutcome.ALREADY_COMPLETED, message=ALREADY_COMPLETED_MESSAGE
        )
    return OperatorActionOutcome(outcome=OperatorRequestOutcome.COMPLETED, message=message)


def _execution_outcome(result: WorkbenchVendorBillExecutionResult) -> OperatorActionOutcome:
    status = result.status
    bill_ids = [
        artifact.artifact_id
        for artifact in result.artifacts
        if artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    ]
    bill = f" (Odoo kayıt {bill_ids[0]})" if len(bill_ids) == 1 else ""
    if status is WorkbenchVendorBillExecutionStatus.EXECUTED:
        return OperatorActionOutcome(
            outcome=OperatorRequestOutcome.COMPLETED,
            message=f"Taslak tedarikçi faturası oluşturuldu{bill}. Faturayı Odoo'da kontrol edip onaylayın.",
        )
    if status is WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED:
        return OperatorActionOutcome(
            outcome=OperatorRequestOutcome.ALREADY_COMPLETED,
            message=f"Tedarikçi faturası daha önce oluşturulmuştu{bill}; tekrar oluşturulmadı.",
        )
    if status is WorkbenchVendorBillExecutionStatus.NOT_FOUND:
        return OperatorActionOutcome(outcome=OperatorRequestOutcome.STALE, message=STALE_REQUEST_MESSAGE)
    detail = f": {result.message}" if result.message else ""
    return OperatorActionOutcome(
        outcome=OperatorRequestOutcome.REJECTED,
        message=f"Fatura oluşturulamadı ({status.value}){detail}",
    )


__all__ = [
    "EXECUTE_VENDOR_BILL_OPERATION",
    "SUPPLIER_AUTHORIZATION_OPERATION",
    "AccountingResolutionRequestHandler",
    "DecisionRequestHandler",
    "ExecuteVendorBillRequestHandler",
    "PurchasePurposeRequestHandler",
    "SupplierResolutionRequestHandler",
    "run_coroutine",
]
