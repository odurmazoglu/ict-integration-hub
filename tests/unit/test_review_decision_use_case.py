from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.execution import ExecutionSourceInvoice
from app.application.execution.exceptions import (
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
)
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
from app.domain.invoice import Header, InternalInvoice, MonetaryTotals, Party
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, PartnerMatchStatus
from app.tax_mapping import InvoiceTaxMappingResult


def test_submit_review_decision_use_case_loads_evidence_for_vendor_bill_and_delegates_atomic_write() -> None:
    command = _select_workflow_command()
    evidence = _source_evidence()
    acknowledgement = ReviewDecisionAcknowledgement(
        accepted=True,
        review_id="review-1",
        status=ReviewStatus.DECISION_SUBMITTED,
        version=2,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
    )
    writer = RecordingDecisionWriter(result=acknowledgement)
    reader = RecordingEvidenceReader(result=evidence)

    result = SubmitReviewDecisionUseCase(review_decision_writer=writer, execution_evidence_reader=reader).execute(
        command
    )

    assert result is acknowledgement
    assert writer.commands == ()
    assert writer.commands_with_evidence == ((command, evidence),)
    assert reader.calls == ({"review_id": "review-1", "company_id": 7, "expected_version": 1},)


def test_submit_review_decision_use_case_delegates_non_executable_decision_without_evidence() -> None:
    command = _select_workflow_command(selected_workflow=WorkflowType.RFQ)
    writer = RecordingDecisionWriter()

    SubmitReviewDecisionUseCase(review_decision_writer=writer).execute(command)

    assert writer.commands == (command,)
    assert writer.commands_with_evidence == ()


def test_submit_review_decision_use_case_requires_evidence_reader_for_vendor_bill() -> None:
    with pytest.raises(ReviewDecisionError) as raised:
        SubmitReviewDecisionUseCase(review_decision_writer=RecordingDecisionWriter()).execute(
            _select_workflow_command()
        )

    assert str(raised.value) == "Execution source evidence is required for Vendor Bill decisions."


def test_submit_review_decision_use_case_missing_evidence_prevents_decision_write() -> None:
    writer = RecordingDecisionWriter()
    error = ExecutionSourceInvoiceNotFoundError("Execution source invoice evidence was not found.")

    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        SubmitReviewDecisionUseCase(
            review_decision_writer=writer,
            execution_evidence_reader=FailingEvidenceReader(error),
        ).execute(_select_workflow_command())

    assert writer.commands == ()
    assert writer.commands_with_evidence == ()


def test_submit_review_decision_use_case_malformed_evidence_prevents_decision_write() -> None:
    writer = RecordingDecisionWriter()
    error = ExecutionSourceInvoiceIntegrityError("Execution source invoice evidence is invalid.")

    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        SubmitReviewDecisionUseCase(
            review_decision_writer=writer,
            execution_evidence_reader=FailingEvidenceReader(error),
        ).execute(_select_workflow_command())

    assert writer.commands == ()
    assert writer.commands_with_evidence == ()


def test_submit_review_decision_use_case_rejects_non_command_input() -> None:
    use_case = SubmitReviewDecisionUseCase(review_decision_writer=RecordingDecisionWriter())

    with pytest.raises(WorkbenchContractError):
        use_case.execute("not-a-command")  # type: ignore[arg-type]


def test_submit_review_decision_use_case_propagates_known_safe_errors() -> None:
    error = ReviewVersionConflictError("Review item version does not match expected_version.")
    use_case = SubmitReviewDecisionUseCase(review_decision_writer=RecordingDecisionWriter(error=error))

    with pytest.raises(ReviewVersionConflictError) as raised:
        use_case.execute(_select_workflow_command(selected_workflow=WorkflowType.RFQ))

    assert raised.value is error


def test_submit_review_decision_use_case_translates_unexpected_errors_safely() -> None:
    sensitive = RuntimeError("sql password=secret token=abc")
    use_case = SubmitReviewDecisionUseCase(review_decision_writer=RecordingDecisionWriter(error=sensitive))

    with pytest.raises(ReviewDecisionError) as raised:
        use_case.execute(_select_workflow_command(selected_workflow=WorkflowType.RFQ))

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
        self.commands_with_evidence: tuple[tuple[ReviewDecisionCommand, ExecutionSourceInvoice], ...] = ()

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

    def submit_review_decision_with_execution_evidence(
        self,
        command: ReviewDecisionCommand,
        evidence: ExecutionSourceInvoice,
    ) -> ReviewDecisionAcknowledgement:
        self.commands_with_evidence = (*self.commands_with_evidence, (command, evidence))
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


class RecordingEvidenceReader:
    def __init__(self, *, result: ExecutionSourceInvoice) -> None:
        self.result = result
        self.calls: tuple[dict[str, object], ...] = ()

    def get_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        expected_version: int,
    ) -> ExecutionSourceInvoice:
        self.calls = (
            *self.calls,
            {"review_id": review_id, "company_id": company_id, "expected_version": expected_version},
        )
        return self.result


class FailingEvidenceReader:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        expected_version: int,
    ) -> ExecutionSourceInvoice:
        raise self.error


def _select_workflow_command(*, selected_workflow: WorkflowType = WorkflowType.VENDOR_BILL) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id="review-1",
        company_id=7,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=selected_workflow,
        decided_by="finance.user",
        idempotency_key="decision-key-1",
    )


def _source_evidence() -> ExecutionSourceInvoice:
    return ExecutionSourceInvoice(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        source_invoice_id="ETTN-1",
        invoice=InternalInvoice(
            header=Header(invoice_number="INV-1", invoice_uuid="ETTN-1", ettn="ETTN-1"),
            supplier=Party(name="Supplier"),
            customer=Party(name="Customer"),
            totals=MonetaryTotals(payable_amount=Decimal("120.00")),
        ),
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=50,
            matched_by="tax_number",
            reason="Matched.",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(),
        tax_match=InvoiceTaxMappingResult(),
    )
