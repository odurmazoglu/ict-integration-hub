from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.dto import ReviewItem, ReviewQueueResult, ReviewStatus
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    WorkbenchContractError,
)
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.models.workbench_review_item import WorkbenchReviewItem

SAFE_PERSISTENCE_ERROR = "Review persistence operation failed."


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


def _validate_create_request(item: ReviewItem, *, company_id: int, idempotency_key: str) -> None:
    if type(company_id) is not int or company_id <= 0:
        raise WorkbenchContractError("company_id must be positive.")
    if idempotency_key is None or not idempotency_key.strip():
        raise WorkbenchContractError("idempotency_key is required.")
    if item.status is not ReviewStatus.PENDING_REVIEW:
        raise WorkbenchContractError("Only PENDING_REVIEW review items can be created.")
    if item.version != 1:
        raise WorkbenchContractError("New review items must start at version 1.")


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
        total_amount=item.total_amount,
        workflow=item.workflow.value,
        status=item.status.value,
        review_reasons=[_serialize_reason(reason) for reason in item.review_reasons],
        warnings=[str(warning) for warning in item.warnings],
        version=1,
        idempotency_key=idempotency_key,
    )


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
        str(item.total_amount) if item.total_amount is not None else None,
        item.workflow,
        item.status,
        tuple(_reason_fingerprint(reason) for reason in item.review_reasons),
        item.warnings,
        item.version,
    )


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
