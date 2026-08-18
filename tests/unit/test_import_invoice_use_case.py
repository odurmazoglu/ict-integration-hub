from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

import app.application
from app.application.commands import ImportInvoiceCommand
from app.application.dto import DecisionResult, ExistingInvoiceImport, ImportInvoiceResult
from app.application.rules import (
    InvoiceClassificationResult,
    InvoiceClassificationRuleEvidence,
    InvoiceClassificationStatus,
    InvoiceDecisionRule,
    InvoiceDecisionRuleAction,
    InvoiceDecisionRuleMatch,
    InvoiceDecisionRulePriority,
)
from app.application.use_cases import (
    WORKBENCH_PROJECTION_FAILURE_WARNING,
    ImportInvoiceInfrastructureError,
    ImportInvoiceUseCase,
    ImportInvoiceValidationError,
)
from app.application.workbench import ReviewClassificationEvidence, ReviewItem
from app.application.workbench.exceptions import (
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateReadError,
    WorkbenchProjectionPublishError,
)
from app.application.workbench.projection import ProjectionPublishResult, WorkbenchProjection
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party


@pytest.mark.asyncio
async def test_import_invoice_delegates_to_decision_engine_after_duplicate_check() -> None:
    decision_engine = FakeDecisionEngine(
        DecisionResult(
            success=True,
            invoice_id="INV-ETTN",
            workflow=WorkflowType.VENDOR_BILL,
            strategy=WorkflowType.VENDOR_BILL.value,
            status="dry_run",
            warnings=("Decision completed.",),
        )
    )
    use_case = _use_case(decision_engine=decision_engine)
    command = ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)

    result = await use_case.execute(command)

    assert result.success is True
    assert result.invoice_id == "INV-ETTN"
    assert result.status == "dry_run"
    assert result.vendor_bill_id is None
    assert result.warnings == ("Decision completed.",)
    assert result.errors == ()
    assert result.duration >= 0
    assert use_case.import_history.calls == ["ettn:INV-ETTN"]
    assert decision_engine.commands == [command]


@pytest.mark.asyncio
async def test_duplicate_import_short_circuits_decision_engine() -> None:
    decision_engine = FakeDecisionEngine()
    use_case = _use_case(
        existing=ExistingInvoiceImport(invoice_id="INV-ETTN", vendor_bill_id=42),
        decision_engine=decision_engine,
    )

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result == ImportInvoiceResult(
        success=True,
        invoice_id="INV-ETTN",
        status="already_imported",
        vendor_bill_id=42,
        warnings=("Invoice was already imported.",),
        duration=result.duration,
    )
    assert decision_engine.commands == []


@pytest.mark.asyncio
async def test_decision_existing_result_is_returned_without_erp_model_leakage() -> None:
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(
            DecisionResult(
                success=True,
                invoice_id="INV-ETTN",
                workflow=WorkflowType.VENDOR_BILL,
                strategy=WorkflowType.VENDOR_BILL.value,
                status="already_exists",
                vendor_bill_id=99,
                warnings=("Existing draft found.",),
            )
        )
    )

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result.success is True
    assert result.status == "already_exists"
    assert result.vendor_bill_id == 99
    assert result.warnings == ("Existing draft found.",)
    assert not hasattr(result, "external_model")


@pytest.mark.asyncio
async def test_decision_failed_result_is_returned_as_safe_failure_result() -> None:
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(
            DecisionResult(
                success=False,
                invoice_id="INV-ETTN",
                workflow=WorkflowType.VENDOR_BILL,
                strategy=WorkflowType.VENDOR_BILL.value,
                status="failed",
                errors=("Vendor Bill write failed safely.",),
            )
        )
    )

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result.success is False
    assert result.status == "failed"
    assert result.errors == ("Vendor Bill write failed safely.",)


@pytest.mark.asyncio
async def test_decision_review_required_result_preserves_structured_reasons() -> None:
    review_reasons = (
        ManualReviewReason(
            code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
            message="Supplier was not matched deterministically.",
            source="partner_matching",
        ),
    )
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(
            DecisionResult(
                success=False,
                invoice_id="INV-ETTN",
                workflow=WorkflowType.MANUAL_REVIEW,
                strategy=WorkflowType.MANUAL_REVIEW.value,
                status="review_required",
                review_required=True,
                review_reasons=review_reasons,
                warnings=("Manual review required.",),
            )
        )
    )

    result = await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert result.success is False
    assert result.status == "review_required"
    assert result.review_required is True
    assert result.review_reasons == review_reasons
    assert result.warnings == ("Manual review required.",)


@pytest.mark.asyncio
async def test_review_required_import_persists_review_before_projection_publish() -> None:
    order: list[str] = []
    review_service = RecordingReviewItemCreationService(order=order)
    unit_of_work = RecordingUnitOfWork(order=order)
    publisher = RecordingProjectionPublisher(order=order)
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(_review_required_decision(classification_result=_matched_classification())),
        review_item_creation_service=review_service,
        workbench_projection_publisher=publisher,
        unit_of_work=unit_of_work,
    )

    result = await use_case.execute(
        ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)
    )

    assert order == ["persist", "commit", "publish"]
    assert result.review_id == publisher.projections[0].review_id
    assert result.review_required is True
    assert review_service.classification_evidence is not None
    assert review_service.classification_evidence.review_id == result.review_id
    assert publisher.projections[0] == WorkbenchProjection(
        review_id=result.review_id,
        company_id=7,
        invoice_id="INV-ETTN",
        version=1,
        status=review_service.created_item.status,
        invoice_number="INV-1",
        supplier_name="Supplier",
        supplier_tax_number="1234567890",
        invoice_date=date(2026, 7, 30),
        currency="TRY",
        total_amount=Decimal("120"),
        workflow=WorkflowType.MANUAL_REVIEW,
        review_reasons=result.review_reasons,
        warnings=("Manual review required.",),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "projection_error",
    (
        WorkbenchCandidateReadError("Odoo Workbench projection lookup failed."),
        WorkbenchProjectionPublishError("Odoo Workbench projection publish failed."),
        WorkbenchCandidateAmbiguityError("Odoo Workbench projection lookup returned multiple records."),
    ),
)
@pytest.mark.asyncio
async def test_expected_projection_failures_are_observable_without_rolling_back_persisted_review(
    projection_error: Exception,
) -> None:
    order: list[str] = []
    review_service = RecordingReviewItemCreationService(order=order)
    unit_of_work = RecordingUnitOfWork(order=order)
    publisher = RecordingProjectionPublisher(order=order, exc=projection_error)
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(_review_required_decision(classification_result=_matched_classification())),
        review_item_creation_service=review_service,
        workbench_projection_publisher=publisher,
        unit_of_work=unit_of_work,
    )

    result = await use_case.execute(
        ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)
    )

    assert order == ["persist", "commit", "publish"]
    assert unit_of_work.rollbacks == 0
    assert result.status == "review_required"
    assert result.review_id == review_service.created_item.review_id
    assert result.warnings == ("Manual review required.", WORKBENCH_PROJECTION_FAILURE_WARNING)


@pytest.mark.parametrize("projection_error", (RuntimeError("bug"), TypeError("wrong shape")))
@pytest.mark.asyncio
async def test_unexpected_projection_errors_are_not_swallowed(projection_error: Exception) -> None:
    order: list[str] = []
    review_service = RecordingReviewItemCreationService(order=order)
    unit_of_work = RecordingUnitOfWork(order=order)
    publisher = RecordingProjectionPublisher(order=order, exc=projection_error)
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(_review_required_decision(classification_result=_matched_classification())),
        review_item_creation_service=review_service,
        workbench_projection_publisher=publisher,
        unit_of_work=unit_of_work,
    )

    with pytest.raises(type(projection_error)):
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7))

    assert order == ["persist", "commit", "publish"]


@pytest.mark.asyncio
async def test_commit_failure_is_translated_and_skips_projection_publish() -> None:
    order: list[str] = []
    review_service = RecordingReviewItemCreationService(order=order)
    unit_of_work = RecordingUnitOfWork(order=order, commit_exc=SafeInfrastructureError("Database commit failed."))
    publisher = RecordingProjectionPublisher(order=order)
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(_review_required_decision(classification_result=_matched_classification())),
        review_item_creation_service=review_service,
        workbench_projection_publisher=publisher,
        unit_of_work=unit_of_work,
    )

    with pytest.raises(ImportInvoiceInfrastructureError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7))

    assert exc_info.value.safe_message == "Database commit failed."
    assert order == ["persist", "commit"]
    assert publisher.projections == []


@pytest.mark.asyncio
async def test_repeated_review_required_import_uses_stable_review_and_projection_identity() -> None:
    publisher = RecordingProjectionPublisher()
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(_review_required_decision(classification_result=_matched_classification())),
        review_item_creation_service=RecordingReviewItemCreationService(),
        workbench_projection_publisher=publisher,
    )
    command = ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)

    first = await use_case.execute(command)
    second = await use_case.execute(command)

    assert first.review_id == second.review_id
    assert [projection.review_id for projection in publisher.projections] == [first.review_id, first.review_id]


@pytest.mark.asyncio
async def test_review_creation_requires_company_scope_when_runtime_wiring_is_enabled() -> None:
    use_case = _use_case(
        decision_engine=FakeDecisionEngine(_review_required_decision(classification_result=_matched_classification())),
        review_item_creation_service=RecordingReviewItemCreationService(),
    )

    with pytest.raises(ImportInvoiceValidationError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert exc_info.value.safe_message == "A positive company_id is required for Workbench review creation."


@pytest.mark.asyncio
async def test_missing_idempotency_key_is_application_validation_error() -> None:
    use_case = _use_case()

    with pytest.raises(ImportInvoiceValidationError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key=" "))

    assert exc_info.value.safe_message == "Import idempotency key is required."
    assert use_case.import_history.calls == []


@pytest.mark.asyncio
async def test_idempotency_key_is_normalized_for_duplicate_check_and_decision_engine() -> None:
    use_case = _use_case()

    await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="  ettn:INV-ETTN  "))

    assert use_case.import_history.calls == ["ettn:INV-ETTN"]
    assert use_case.decision_engine.commands[0].idempotency_key == "ettn:INV-ETTN"


@pytest.mark.asyncio
async def test_infrastructure_exceptions_are_translated_to_application_errors() -> None:
    use_case = _use_case(import_history=FailingImportHistory())

    with pytest.raises(ImportInvoiceInfrastructureError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert exc_info.value.safe_message == "History lookup unavailable."


@pytest.mark.asyncio
async def test_unexpected_decision_exception_is_translated_to_application_error() -> None:
    use_case = _use_case(decision_engine=FakeDecisionEngine(RuntimeError("transport details")))

    with pytest.raises(ImportInvoiceInfrastructureError) as exc_info:
        await use_case.execute(ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN"))

    assert exc_info.value.safe_message == "Decision Engine execution failed."


def test_import_invoice_dtos_are_immutable() -> None:
    command = ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN")
    result = ImportInvoiceResult(success=True, invoice_id="INV-ETTN", status="dry_run")

    with pytest.raises(FrozenInstanceError):
        command.dry_run = False
    with pytest.raises(FrozenInstanceError):
        result.status = "created"


def test_application_package_exports_import_invoice_use_case() -> None:
    assert app.application.ImportInvoiceUseCase is ImportInvoiceUseCase


class FakeImportHistory:
    def __init__(self, existing: ExistingInvoiceImport | None = None) -> None:
        self.existing = existing
        self.calls: list[str] = []

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        self.calls.append(idempotency_key)
        return self.existing


class FailingImportHistory:
    calls: list[str] = []

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        self.calls.append(idempotency_key)
        raise SafeInfrastructureError("History lookup unavailable.")


class SafeInfrastructureError(Exception):
    def __init__(self, safe_message: str) -> None:
        super().__init__("unsafe provider details")
        self.safe_message = safe_message


class RecordingReviewItemCreationService:
    def __init__(self, *, order: list[str] | None = None) -> None:
        self.order = order
        self.created_item: ReviewItem | None = None
        self.classification_evidence: ReviewClassificationEvidence | None = None

    def create_pending_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        self.order.append("persist") if self.order is not None else None
        self.created_item = item
        return item

    def create_pending_review_item_with_classification_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        classification_evidence: ReviewClassificationEvidence,
    ) -> ReviewItem:
        self.order.append("persist") if self.order is not None else None
        self.created_item = item
        self.classification_evidence = classification_evidence
        return item


class RecordingUnitOfWork:
    def __init__(self, *, order: list[str] | None = None, commit_exc: Exception | None = None) -> None:
        self.order = order
        self.commit_exc = commit_exc
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.order.append("commit") if self.order is not None else None
        self.commits += 1
        if self.commit_exc is not None:
            raise self.commit_exc

    def rollback(self) -> None:
        self.order.append("rollback") if self.order is not None else None
        self.rollbacks += 1


class RecordingProjectionPublisher:
    def __init__(self, *, order: list[str] | None = None, exc: Exception | None = None) -> None:
        self.order = order
        self.exc = exc
        self.projections: list[WorkbenchProjection] = []

    def publish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        self.order.append("publish") if self.order is not None else None
        self.projections.append(projection)
        if self.exc is not None:
            raise self.exc
        return ProjectionPublishResult(
            review_id=projection.review_id,
            odoo_record_id=101,
            created=True,
            updated=False,
            version=projection.version,
        )

    def acknowledge_decision(self, *args: object, **kwargs: object) -> ProjectionPublishResult:
        raise AssertionError("acknowledgement publishing is not part of import runtime wiring")


class FakeDecisionEngine:
    def __init__(self, result: DecisionResult | Exception | None = None) -> None:
        self.result = result or DecisionResult(
            success=True,
            invoice_id="INV-ETTN",
            workflow=WorkflowType.VENDOR_BILL,
            strategy=WorkflowType.VENDOR_BILL.value,
            status="dry_run",
        )
        self.commands: list[ImportInvoiceCommand] = []

    async def decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        self.commands.append(command)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class UseCaseFixture(ImportInvoiceUseCase):
    import_history: FakeImportHistory | FailingImportHistory
    decision_engine: FakeDecisionEngine


def _use_case(
    *,
    existing: ExistingInvoiceImport | None = None,
    import_history: FakeImportHistory | FailingImportHistory | None = None,
    decision_engine: FakeDecisionEngine | None = None,
    review_item_creation_service: RecordingReviewItemCreationService | None = None,
    workbench_projection_publisher: RecordingProjectionPublisher | None = None,
    unit_of_work: RecordingUnitOfWork | None = None,
) -> UseCaseFixture:
    history = import_history or FakeImportHistory(existing)
    engine = decision_engine or FakeDecisionEngine()
    use_case = UseCaseFixture(
        import_history=history,
        decision_engine=engine,  # type: ignore[arg-type]
        review_item_creation_service=review_item_creation_service,  # type: ignore[arg-type]
        workbench_projection_publisher=workbench_projection_publisher,  # type: ignore[arg-type]
        unit_of_work=unit_of_work,  # type: ignore[arg-type]
    )
    use_case.import_history = history
    use_case.decision_engine = engine
    return use_case


def _review_required_decision(*, classification_result: InvoiceClassificationResult | None = None) -> DecisionResult:
    return DecisionResult(
        success=False,
        invoice_id="INV-ETTN",
        workflow=WorkflowType.MANUAL_REVIEW,
        strategy=WorkflowType.MANUAL_REVIEW.value,
        status="review_required",
        review_required=True,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
                message="Supplier was not matched deterministically.",
                source="partner_matching",
            ),
        ),
        classification_result=classification_result,
        warnings=("Manual review required.",),
    )


def _matched_classification() -> InvoiceClassificationResult:
    rule = InvoiceDecisionRule(
        rule_id="odoo:1",
        rule_code="RULE-CLOUD",
        rule_version=3,
        name="Cloud Cost",
        enabled=True,
        priority=InvoiceDecisionRulePriority(tier=10),
        match=InvoiceDecisionRuleMatch(vendor_tax_id="1234567890"),
        action=InvoiceDecisionRuleAction(
            workflow=WorkflowType.MANUAL_REVIEW,
            classification_code="CLOUD_COST",
            require_review=True,
            require_business_context=True,
        ),
    )
    evidence = InvoiceClassificationRuleEvidence.from_rule(rule)
    return InvoiceClassificationResult(
        status=InvoiceClassificationStatus.REVIEW_REQUIRED,
        matched_rules=(rule,),
        selected_rule=rule,
        matched_rule_evidence=(evidence,),
    )


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="INV-UUID",
            ettn="INV-ETTN",
            issue_date=date(2026, 7, 30),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Line 1",
                buyer_item_code="SKU-1",
                quantity=Decimal("2"),
                unit_code="NIU",
                unit_price=Decimal("50"),
            ),
        ),
    )
