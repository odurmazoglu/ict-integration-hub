from __future__ import annotations

import asyncio

from app.application.exceptions import ApplicationError
from app.application.execution.contracts import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    ExecutionApprovalError,
    ExecutionPlanningError,
    ExecutionUnsupportedStepError,
)
from app.application.quotation.contracts import QuotationScenarioSnapshot
from app.application.quotation.evidence import QuotationScenarioEvidenceRepository
from app.application.quotation.exceptions import QuotationEvidenceDataIntegrityError, QuotationEvidenceError
from app.application.quotation.execution import (
    CreateCustomerQuotationCommand,
    CustomerQuotationDraft,
    CustomerQuotationWriter,
)


class CustomerQuotationExecutionStrategy:
    """Create one draft Odoo customer Sales Quotation from one immutable scenario.

    Execution input is the persisted :class:`QuotationScenarioSnapshot` loaded from
    Hub quotation evidence by semantic identity. The strategy never reads Odoo
    Proposal Scenario authoring records, never auto-captures, and never reaches
    Odoo directly — it uses the :class:`CustomerQuotationWriter` port only.
    """

    name = "customer_quotation_execution"
    supported_step_types = (ExecutionStepType.CREATE_CUSTOMER_QUOTATION,)

    def __init__(
        self,
        *,
        quotation_evidence_reader: QuotationScenarioEvidenceRepository,
        customer_quotation_writer: CustomerQuotationWriter,
    ) -> None:
        self._quotation_evidence_reader = quotation_evidence_reader
        self._customer_quotation_writer = customer_quotation_writer

    def supports_mode(self, mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE}

    def supports_step(self, *, step: object, mode: ExecutionMode) -> bool:
        is_quotation_step = getattr(step, "step_type", None) is ExecutionStepType.CREATE_CUSTOMER_QUOTATION
        return is_quotation_step and self.supports_mode(mode)

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        if request.step.step_type is not ExecutionStepType.CREATE_CUSTOMER_QUOTATION:
            raise ExecutionUnsupportedStepError(
                "CustomerQuotationExecutionStrategy only supports CREATE_CUSTOMER_QUOTATION steps."
            )
        if request.mode is ExecutionMode.EXECUTE and request.approval is None:
            raise ExecutionApprovalError("Explicit execution approval is required for customer quotation execution.")

        scenario_id = request.step.customer_quotation_scenario_id
        if not scenario_id:
            return _failure_result(
                request,
                error_code="customer_quotation_scenario_missing",
                message="Execution step is missing its customer quotation scenario id.",
            )
        decision_id = request.decision_id
        if not decision_id:
            return _failure_result(
                request,
                error_code="customer_quotation_decision_identity_missing",
                message="Accepted decision identity is required to load customer quotation evidence.",
            )

        try:
            snapshot = self._quotation_evidence_reader.get(
                company_id=request.company_id,
                review_id=request.review_id,
                decision_id=decision_id,
                decision_version=request.decision_version,
                scenario_id=scenario_id,
            )
            _validate_snapshot(request=request, snapshot=snapshot, scenario_id=scenario_id)
            draft = CustomerQuotationDraft.from_snapshot(snapshot)
            if request.mode is ExecutionMode.DRY_RUN:
                return ExecutionStepResult(
                    step_key=request.step.step_key,
                    step_type=request.step.step_type,
                    status=ExecutionStepStatus.DRY_RUN_OK,
                    dry_run=True,
                    message="Dry run completed. No Odoo sale.order was created.",
                )
            write_result = _run_writer(
                self._customer_quotation_writer,
                CreateCustomerQuotationCommand(
                    draft=draft,
                    approved_by=request.approval.approved_by if request.approval is not None else None,
                ),
            )
        except QuotationEvidenceError as exc:
            return _failure_result(request, error_code=exc.error_category, message=exc.safe_message)
        except ApplicationError as exc:
            return _failure_result(request, error_code=_writer_error_code(exc), message=exc.safe_message)

        message = (
            "Draft customer quotation created in Odoo."
            if write_result.created
            else "Draft customer quotation already exists in Odoo."
        )
        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=ExecutionStepStatus.EXECUTED,
            dry_run=False,
            message=message,
            produced_artifacts=(
                ExecutionArtifact(
                    artifact_type=ExecutionArtifactType.CUSTOMER_QUOTATION,
                    artifact_id=str(write_result.external_quotation_id),
                    external_identity=write_result.execution_key,
                    created=write_result.created,
                ),
            ),
        )


def _validate_snapshot(
    *,
    request: ExecutionStepRequest,
    snapshot: QuotationScenarioSnapshot,
    scenario_id: str,
) -> None:
    identity = (
        snapshot.company_id,
        snapshot.review_id,
        snapshot.decision_id,
        snapshot.decision_version,
        snapshot.scenario_id,
    )
    expected = (
        request.company_id,
        request.review_id,
        request.decision_id,
        request.decision_version,
        scenario_id,
    )
    if identity != expected:
        raise QuotationEvidenceDataIntegrityError(
            "Quotation scenario evidence identity does not match the execution request."
        )


def _run_writer(writer: CustomerQuotationWriter, command: CreateCustomerQuotationCommand):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(writer.create_quotation(command))
    raise ExecutionPlanningError("Customer quotation writer cannot run inside an active event loop.")


def _failure_result(request: ExecutionStepRequest, *, error_code: str, message: str) -> ExecutionStepResult:
    return ExecutionStepResult(
        step_key=request.step.step_key,
        step_type=request.step.step_type,
        status=ExecutionStepStatus.FAILED,
        dry_run=request.mode is ExecutionMode.DRY_RUN,
        message=message,
        error_code=error_code,
    )


def _writer_error_code(exc: ApplicationError) -> str:
    category = exc.error_category
    mapping = {
        "production_safety_gate_failure": "customer_quotation_safety_gate_failure",
        "configuration_failure": "customer_quotation_configuration_failure",
        "pricelist_resolution_failure": "customer_quotation_pricelist_resolution_failure",
        "authentication_failure": "customer_quotation_authentication_failure",
        "authorization_failure": "customer_quotation_authorization_failure",
        "validation_failure": "customer_quotation_validation_failure",
        "duplicate_detection_failure": "customer_quotation_duplicate_detection_failure",
        "transport_failure": "customer_quotation_transport_failure",
    }
    return mapping.get(category, "customer_quotation_write_error")
