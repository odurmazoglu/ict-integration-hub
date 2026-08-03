from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.exceptions import ApplicationError
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import (
    BusinessContextDecision,
    LineResolution,
    ReviewDecisionAcknowledgement,
    ReviewDecisionType,
    ReviewItem,
    ReviewQueueResult,
    ReviewStatus,
    TaxResolution,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewDecisionDataIntegrityError,
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import REVIEW_AMOUNT_PRECISION, REVIEW_AMOUNT_SCALE, WorkbenchReviewItem

SAFE_PERSISTENCE_ERROR = "Review persistence operation failed."
SAFE_DECISION_ERROR = "Review decision persistence operation failed."
REVIEW_AMOUNT_INTEGER_DIGITS = REVIEW_AMOUNT_PRECISION - REVIEW_AMOUNT_SCALE


class SqlAlchemyReviewRepository:
    """SQLAlchemy adapter for idempotent review item creation and queue reads."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        _validate_create_request(item, company_id=company_id, idempotency_key=idempotency_key)
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
            if existing is not None:
                return self._return_existing_or_raise_conflict(existing, item, company_id=company_id)

            record = _model_from_review_item(item, company_id=company_id, idempotency_key=idempotency_key)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return _review_item_from_model(record)
        except IntegrityError as exc:
            return self._handle_create_integrity_error(
                item,
                company_id=company_id,
                idempotency_key=idempotency_key,
                exc=exc,
            )
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        try:
            filters = _query_filters(query)
            total_count = self._session.scalar(select(func.count()).select_from(WorkbenchReviewItem).where(*filters))
            records = tuple(
                self._session.scalars(
                    select(WorkbenchReviewItem)
                    .where(*filters)
                    .order_by(WorkbenchReviewItem.created_at.asc(), WorkbenchReviewItem.review_id.asc())
                    .offset(query.offset)
                    .limit(query.limit)
                )
            )
            return ReviewQueueResult(
                items=tuple(_review_item_from_model(record) for record in records),
                total_count=int(total_count or 0),
                limit=query.limit,
                offset=query.offset,
            )
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        try:
            record = self._session.scalar(
                select(WorkbenchReviewItem).where(
                    WorkbenchReviewItem.review_id == query.review_id,
                    WorkbenchReviewItem.company_id == query.company_id,
                )
            )
            if record is None:
                raise ReviewNotFoundError("Review item was not found.")
            return _review_item_from_model(record)
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def submit_review_decision(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        try:
            existing = self._find_decision_by_idempotency_key(
                company_id=command.company_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                return self._return_existing_decision_or_raise_conflict(existing, command)

            target_status = _target_status_for_decision(command.decision)
            decision = _decision_model_from_command(
                command,
                decision_id=f"review-decision:{uuid4()}",
            )
            with self._session.begin_nested():
                result = self._session.execute(
                    update(WorkbenchReviewItem)
                    .where(
                        WorkbenchReviewItem.review_id == command.review_id,
                        WorkbenchReviewItem.company_id == command.company_id,
                        WorkbenchReviewItem.status == ReviewStatus.PENDING_REVIEW.value,
                        WorkbenchReviewItem.version == command.expected_version,
                    )
                    .values(
                        status=target_status.value,
                        version=command.expected_version + 1,
                        updated_at=func.now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(result.rowcount or 0) != 1:
                    self._raise_submission_conflict(command)
                self._session.add(decision)
                self._session.flush()
                self._session.refresh(decision)
            return _acknowledgement_from_decision_model(decision)
        except IntegrityError as exc:
            return self._handle_decision_integrity_error(command, exc=exc)
        except ApplicationError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc

    def _find_by_idempotency_key(
        self,
        *,
        company_id: int,
        idempotency_key: str,
    ) -> WorkbenchReviewItem | None:
        return self._session.scalar(
            select(WorkbenchReviewItem).where(
                WorkbenchReviewItem.company_id == company_id,
                WorkbenchReviewItem.idempotency_key == idempotency_key,
            )
        )

    def _return_existing_or_raise_conflict(
        self,
        existing: WorkbenchReviewItem,
        item: ReviewItem,
        *,
        company_id: int,
    ) -> ReviewItem:
        existing_item = _review_item_from_model(existing)
        if _business_fingerprint(existing_item, company_id=company_id) != _business_fingerprint(
            item,
            company_id=company_id,
        ):
            raise ReviewIdempotencyConflictError("Review idempotency key conflicts with an existing review item.")
        return existing_item

    def _handle_create_integrity_error(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        exc: IntegrityError,
    ) -> ReviewItem:
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
        except SQLAlchemyError as lookup_exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_raise_conflict(existing, item, company_id=company_id)
            except ReviewPersistenceError as conflict_exc:
                raise conflict_exc from exc

        try:
            same_review_id = self._session.scalar(
                select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == item.review_id)
            )
        except SQLAlchemyError as lookup_exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from lookup_exc
        if same_review_id is not None:
            raise ReviewDataIntegrityError("Review item already exists.") from exc
        raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def _find_decision_by_idempotency_key(
        self,
        *,
        company_id: int,
        idempotency_key: str,
    ) -> WorkbenchReviewDecision | None:
        return self._session.scalar(
            select(WorkbenchReviewDecision).where(
                WorkbenchReviewDecision.company_id == company_id,
                WorkbenchReviewDecision.idempotency_key == idempotency_key,
            )
        )

    def _return_existing_decision_or_raise_conflict(
        self,
        existing: WorkbenchReviewDecision,
        command: ReviewDecisionCommand,
    ) -> ReviewDecisionAcknowledgement:
        if _decision_fingerprint_from_model(existing) != _decision_fingerprint(command):
            raise ReviewDecisionIdempotencyConflictError(
                "Review decision idempotency key conflicts with an existing decision."
            )
        return _acknowledgement_from_decision_model(existing)

    def _raise_submission_conflict(self, command: ReviewDecisionCommand) -> None:
        try:
            record = self._session.scalar(
                select(WorkbenchReviewItem).where(
                    WorkbenchReviewItem.review_id == command.review_id,
                    WorkbenchReviewItem.company_id == command.company_id,
                )
            )
        except SQLAlchemyError as exc:
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc
        if record is None:
            raise ReviewNotFoundError("Review item was not found.")
        if record.status != ReviewStatus.PENDING_REVIEW.value:
            raise ReviewStateConflictError("Review item is no longer pending review.")
        if record.version != command.expected_version:
            raise ReviewVersionConflictError("Review item version does not match expected_version.")
        raise ReviewStateConflictError("Review item could not accept the decision.")

    def _handle_decision_integrity_error(
        self,
        command: ReviewDecisionCommand,
        *,
        exc: IntegrityError,
    ) -> ReviewDecisionAcknowledgement:
        try:
            existing = self._find_decision_by_idempotency_key(
                company_id=command.company_id,
                idempotency_key=command.idempotency_key,
            )
        except SQLAlchemyError as lookup_exc:
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_decision_or_raise_conflict(existing, command)
            except ReviewDecisionError as conflict_exc:
                raise conflict_exc from exc
        raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc


def _validate_create_request(item: ReviewItem, *, company_id: int, idempotency_key: str) -> None:
    if type(company_id) is not int or company_id <= 0:
        raise WorkbenchContractError("company_id must be positive.")
    if idempotency_key is None or not idempotency_key.strip():
        raise WorkbenchContractError("idempotency_key is required.")
    if item.status is not ReviewStatus.PENDING_REVIEW:
        raise WorkbenchContractError("Only PENDING_REVIEW review items can be created.")
    if item.version != 1:
        raise WorkbenchContractError("New review items must start at version 1.")
    _validate_supported_amount(item.total_amount)


def _query_filters(query: ReviewQueueQuery) -> list[Any]:
    filters: list[Any] = [
        WorkbenchReviewItem.company_id == query.company_id,
        WorkbenchReviewItem.status == query.status.value,
    ]
    if query.supplier_tax_number is not None:
        filters.append(WorkbenchReviewItem.supplier_tax_number == query.supplier_tax_number)
    if query.workflow is not None:
        filters.append(WorkbenchReviewItem.workflow == query.workflow.value)
    if query.created_from is not None:
        filters.append(WorkbenchReviewItem.created_at >= query.created_from)
    if query.created_to is not None:
        filters.append(WorkbenchReviewItem.created_at <= query.created_to)
    return filters


def _model_from_review_item(
    item: ReviewItem,
    *,
    company_id: int,
    idempotency_key: str,
) -> WorkbenchReviewItem:
    return WorkbenchReviewItem(
        review_id=item.review_id,
        company_id=company_id,
        invoice_id=item.invoice_id,
        invoice_number=item.invoice_number,
        supplier_tax_number=item.supplier_tax_number,
        supplier_name=item.supplier_name,
        invoice_date=item.invoice_date,
        currency=item.currency,
        total_amount=_canonical_decimal(item.total_amount),
        workflow=item.workflow.value,
        status=item.status.value,
        review_reasons=[_serialize_reason(reason) for reason in item.review_reasons],
        warnings=[str(warning) for warning in item.warnings],
        version=1,
        idempotency_key=idempotency_key,
    )


def _decision_model_from_command(
    command: ReviewDecisionCommand,
    *,
    decision_id: str,
) -> WorkbenchReviewDecision:
    return WorkbenchReviewDecision(
        decision_id=decision_id,
        review_id=command.review_id,
        company_id=command.company_id,
        review_version_before=command.expected_version,
        review_version_after=command.expected_version + 1,
        decision_type=command.decision.value,
        selected_workflow=command.selected_workflow.value if command.selected_workflow is not None else None,
        selected_partner_id=command.selected_partner_id,
        line_resolutions=[_serialize_line_resolution(resolution) for resolution in command.line_resolutions],
        tax_resolutions=[_serialize_tax_resolution(resolution) for resolution in command.tax_resolutions],
        business_context=_serialize_business_context(command.business_context),
        comment=command.comment,
        decided_by=command.decided_by,
        idempotency_key=command.idempotency_key,
    )


def _acknowledgement_from_decision_model(record: WorkbenchReviewDecision) -> ReviewDecisionAcknowledgement:
    try:
        decision = ReviewDecisionType(record.decision_type)
        selected_workflow = WorkflowType(record.selected_workflow) if record.selected_workflow is not None else None
        return ReviewDecisionAcknowledgement(
            accepted=True,
            review_id=record.review_id,
            status=_target_status_for_decision(decision),
            version=record.review_version_after,
            decision=decision,
            selected_workflow=selected_workflow,
        )
    except ReviewDecisionDataIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.") from exc


def _review_item_from_model(record: WorkbenchReviewItem) -> ReviewItem:
    try:
        return ReviewItem(
            review_id=record.review_id,
            invoice_id=record.invoice_id,
            invoice_number=record.invoice_number,
            supplier_tax_number=record.supplier_tax_number,
            supplier_name=record.supplier_name,
            invoice_date=record.invoice_date,
            currency=record.currency,
            total_amount=record.total_amount,
            workflow=WorkflowType(record.workflow),
            status=ReviewStatus(record.status),
            review_reasons=tuple(_deserialize_reason(reason) for reason in _require_list(record.review_reasons)),
            warnings=tuple(_deserialize_warnings(record.warnings)),
            created_at=record.created_at,
            updated_at=record.updated_at,
            version=record.version,
        )
    except ReviewDataIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewDataIntegrityError("Persisted review item data is invalid.") from exc


def _serialize_reason(reason: ManualReviewReason) -> dict[str, Any]:
    return {
        "code": reason.code.value,
        "message": reason.message,
        "line_number": reason.line_number,
        "tax_index": reason.tax_index,
        "candidate_count": reason.candidate_count,
        "source": reason.source,
        "details": [[str(key), str(value)] for key, value in reason.details],
    }


def _serialize_line_resolution(resolution: LineResolution) -> dict[str, Any]:
    return {
        "line_number": resolution.line_number,
        "selected_product_id": resolution.selected_product_id,
    }


def _deserialize_line_resolution(value: Any) -> LineResolution:
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return LineResolution(
        line_number=str(value["line_number"]),
        selected_product_id=_required_int(value.get("selected_product_id")),
    )


def _serialize_tax_resolution(resolution: TaxResolution) -> dict[str, Any]:
    return {
        "line_number": resolution.line_number,
        "tax_index": resolution.tax_index,
        "selected_tax_id": resolution.selected_tax_id,
    }


def _deserialize_tax_resolution(value: Any) -> TaxResolution:
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return TaxResolution(
        line_number=str(value["line_number"]),
        tax_index=_required_int(value.get("tax_index")),
        selected_tax_id=_required_int(value.get("selected_tax_id")),
    )


def _serialize_business_context(context: BusinessContextDecision | None) -> dict[str, int] | None:
    if context is None:
        return None
    return {
        key: value
        for key, value in {
            "opportunity_id": context.opportunity_id,
            "sales_order_id": context.sales_order_id,
            "proposal_scenario_id": context.proposal_scenario_id,
            "purchase_order_id": context.purchase_order_id,
            "project_id": context.project_id,
            "analytic_account_id": context.analytic_account_id,
        }.items()
        if value is not None
    }


def _deserialize_business_context(value: Any) -> BusinessContextDecision | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return BusinessContextDecision(
        opportunity_id=_optional_int(value.get("opportunity_id")),
        sales_order_id=_optional_int(value.get("sales_order_id")),
        proposal_scenario_id=_optional_int(value.get("proposal_scenario_id")),
        purchase_order_id=_optional_int(value.get("purchase_order_id")),
        project_id=_optional_int(value.get("project_id")),
        analytic_account_id=_optional_int(value.get("analytic_account_id")),
    )


def _deserialize_reason(value: Any) -> ManualReviewReason:
    if not isinstance(value, dict):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    details = value.get("details", [])
    if not isinstance(details, list):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return ManualReviewReason(
        code=ManualReviewReasonCode(str(value["code"])),
        message=str(value["message"]),
        line_number=_optional_text(value.get("line_number")),
        tax_index=_optional_int(value.get("tax_index")),
        candidate_count=_optional_int(value.get("candidate_count")),
        source=_optional_text(value.get("source")),
        details=tuple(_detail_pair(pair) for pair in details),
    )


def _deserialize_warnings(value: Any) -> Iterable[str]:
    for warning in _require_list(value):
        if not isinstance(warning, str):
            raise ReviewDataIntegrityError("Persisted review item data is invalid.")
        yield warning


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return value


def _required_int(value: Any) -> int:
    if type(value) is not int:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return value


def _detail_pair(value: Any) -> tuple[str, str]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    key, detail_value = value
    if not isinstance(key, str) or not isinstance(detail_value, str):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return key, detail_value


def _business_fingerprint(item: ReviewItem, *, company_id: int) -> tuple[Any, ...]:
    return (
        company_id,
        item.review_id,
        item.invoice_id,
        item.invoice_number,
        item.supplier_tax_number,
        item.supplier_name,
        item.invoice_date,
        item.currency,
        _canonical_decimal(item.total_amount),
        item.workflow,
        item.status,
        tuple(_reason_fingerprint(reason) for reason in item.review_reasons),
        item.warnings,
        item.version,
    )


def _decision_fingerprint(command: ReviewDecisionCommand) -> tuple[Any, ...]:
    return (
        command.company_id,
        command.review_id,
        command.expected_version,
        command.decision,
        command.selected_workflow,
        command.selected_partner_id,
        tuple(_line_resolution_fingerprint(resolution) for resolution in command.line_resolutions),
        tuple(_tax_resolution_fingerprint(resolution) for resolution in command.tax_resolutions),
        _business_context_fingerprint(command.business_context),
        command.comment,
        command.decided_by,
    )


def _decision_fingerprint_from_model(record: WorkbenchReviewDecision) -> tuple[Any, ...]:
    try:
        command_like = ReviewDecisionCommand(
            review_id=record.review_id,
            company_id=record.company_id,
            expected_version=record.review_version_before,
            decision=ReviewDecisionType(record.decision_type),
            selected_workflow=WorkflowType(record.selected_workflow) if record.selected_workflow is not None else None,
            selected_partner_id=record.selected_partner_id,
            line_resolutions=tuple(
                _deserialize_line_resolution(resolution) for resolution in _require_list(record.line_resolutions)
            ),
            tax_resolutions=tuple(
                _deserialize_tax_resolution(resolution) for resolution in _require_list(record.tax_resolutions)
            ),
            business_context=_deserialize_business_context(record.business_context),
            comment=record.comment,
            decided_by=record.decided_by,
            idempotency_key=record.idempotency_key,
        )
    except ReviewDecisionDataIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.") from exc
    return _decision_fingerprint(command_like)


def _line_resolution_fingerprint(resolution: LineResolution) -> tuple[str, int]:
    return resolution.line_number, resolution.selected_product_id


def _tax_resolution_fingerprint(resolution: TaxResolution) -> tuple[str, int, int]:
    return resolution.line_number, resolution.tax_index, resolution.selected_tax_id


def _business_context_fingerprint(context: BusinessContextDecision | None) -> tuple[tuple[str, int], ...] | None:
    serialized = _serialize_business_context(context)
    if serialized is None:
        return None
    return tuple((key, serialized[key]) for key in sorted(serialized))


def _target_status_for_decision(decision: ReviewDecisionType) -> ReviewStatus:
    if decision is ReviewDecisionType.SELECT_WORKFLOW:
        return ReviewStatus.DECISION_SUBMITTED
    if decision is ReviewDecisionType.DISMISS:
        return ReviewStatus.DISMISSED
    raise WorkbenchContractError("Unsupported review decision.")


def _reason_fingerprint(reason: ManualReviewReason) -> tuple[Any, ...]:
    serialized = _serialize_reason(reason)
    return (
        serialized["code"],
        serialized["message"],
        serialized["line_number"],
        serialized["tax_index"],
        serialized["candidate_count"],
        serialized["source"],
        tuple(tuple(pair) for pair in serialized["details"]),
    )


def _canonical_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite():
        raise WorkbenchContractError("total_amount must be a finite Decimal value.")
    try:
        normalized = value.normalize()
    except InvalidOperation as exc:
        raise WorkbenchContractError("total_amount must be a valid Decimal value.") from exc
    if normalized == Decimal("0"):
        return Decimal("0")
    return normalized


def _validate_supported_amount(value: Decimal | None) -> None:
    canonical = _canonical_decimal(value)
    if canonical is None:
        return
    if _decimal_scale(canonical) > REVIEW_AMOUNT_SCALE:
        raise WorkbenchContractError(f"total_amount supports at most {REVIEW_AMOUNT_SCALE} fractional digits.")
    if _decimal_integer_digits(canonical) > REVIEW_AMOUNT_INTEGER_DIGITS:
        raise WorkbenchContractError(f"total_amount supports at most {REVIEW_AMOUNT_PRECISION} total digits.")


def _decimal_scale(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return abs(exponent) if exponent < 0 else 0


def _decimal_integer_digits(value: Decimal) -> int:
    adjusted = abs(value).adjusted()
    return adjusted + 1 if adjusted >= 0 else 0
