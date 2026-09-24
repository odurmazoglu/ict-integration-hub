from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import (
    PurchasePurposeConflictError,
    PurchasePurposeError,
    WorkbenchContractError,
)
from app.application.workbench.purchase_purpose import PurchasePurpose, PurchasePurposeResolution
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution

SAFE_PURCHASE_PURPOSE_PERSISTENCE_ERROR = "Purchase purpose resolution persistence operation failed."


def _fingerprint(resolution: PurchasePurposeResolution) -> tuple[Any, ...]:
    return (
        resolution.review_id,
        resolution.company_id,
        resolution.review_version,
        resolution.source_invoice_id,
        resolution.purchase_purpose.value,
        (resolution.approved_by or None),
        (resolution.note or None),
    )


def _model_from_resolution(resolution: PurchasePurposeResolution) -> WorkbenchReviewPurchasePurposeResolution:
    return WorkbenchReviewPurchasePurposeResolution(
        review_id=resolution.review_id,
        company_id=resolution.company_id,
        review_version=resolution.review_version,
        source_invoice_id=resolution.source_invoice_id,
        purchase_purpose=resolution.purchase_purpose.value,
        approved_by=resolution.approved_by,
        note=resolution.note,
    )


def _resolution_from_model(record: WorkbenchReviewPurchasePurposeResolution) -> PurchasePurposeResolution:
    try:
        purpose = PurchasePurpose(str(record.purchase_purpose))
    except ValueError as exc:
        raise WorkbenchContractError("Persisted purchase purpose resolution is not canonical.") from exc
    return PurchasePurposeResolution(
        id=int(record.id),
        review_id=str(record.review_id),
        company_id=int(record.company_id),
        review_version=int(record.review_version),
        source_invoice_id=str(record.source_invoice_id),
        purchase_purpose=purpose,
        approved_by=record.approved_by,
        note=record.note,
    )


class SqlAlchemyReviewPurchasePurposeResolutionRepository:
    """Append-only persistence for a review-scoped purchase-purpose resolution.

    One resolution per ``(review_id, review_version)``. A byte-identical retry
    returns the existing row; a different resolution for the same review version
    fails closed. There is no UPDATE path.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_purchase_purpose_resolution(self, resolution: PurchasePurposeResolution) -> PurchasePurposeResolution:
        if not isinstance(resolution, PurchasePurposeResolution):
            raise WorkbenchContractError("A canonical PurchasePurposeResolution is required.")
        try:
            existing = self._find(
                review_id=resolution.review_id,
                company_id=resolution.company_id,
                review_version=resolution.review_version,
            )
            if existing is not None:
                return self._return_existing_or_conflict(existing, resolution)

            record = _model_from_resolution(resolution)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return _resolution_from_model(record)
        except IntegrityError as exc:
            return self._handle_integrity_error(resolution, exc=exc)
        except PurchasePurposeError:
            raise
        except SQLAlchemyError as exc:
            raise PurchasePurposeError(SAFE_PURCHASE_PURPOSE_PERSISTENCE_ERROR) from exc

    def find_purchase_purpose_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> PurchasePurposeResolution | None:
        if not isinstance(review_id, str) or not review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        if type(review_version) is not int or review_version <= 0:
            raise WorkbenchContractError("review_version must be positive.")
        try:
            record = self._find(review_id=review_id, company_id=company_id, review_version=review_version)
        except SQLAlchemyError as exc:
            raise PurchasePurposeError(SAFE_PURCHASE_PURPOSE_PERSISTENCE_ERROR) from exc
        return _resolution_from_model(record) if record is not None else None

    def list_purchase_purpose_resolutions(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> tuple[PurchasePurposeResolution, ...]:
        """Every purchase purpose recorded for one review, oldest review version first."""

        if not isinstance(review_id, str) or not review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        try:
            records = self._session.scalars(
                select(WorkbenchReviewPurchasePurposeResolution)
                .where(
                    WorkbenchReviewPurchasePurposeResolution.review_id == review_id,
                    WorkbenchReviewPurchasePurposeResolution.company_id == company_id,
                )
                .order_by(WorkbenchReviewPurchasePurposeResolution.review_version)
            ).all()
        except SQLAlchemyError as exc:
            raise PurchasePurposeError(SAFE_PURCHASE_PURPOSE_PERSISTENCE_ERROR) from exc
        return tuple(_resolution_from_model(record) for record in records)

    def _find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchReviewPurchasePurposeResolution | None:
        return self._session.scalar(
            select(WorkbenchReviewPurchasePurposeResolution).where(
                WorkbenchReviewPurchasePurposeResolution.review_id == review_id,
                WorkbenchReviewPurchasePurposeResolution.company_id == company_id,
                WorkbenchReviewPurchasePurposeResolution.review_version == review_version,
            )
        )

    def _return_existing_or_conflict(
        self,
        existing: WorkbenchReviewPurchasePurposeResolution,
        resolution: PurchasePurposeResolution,
    ) -> PurchasePurposeResolution:
        existing_resolution = _resolution_from_model(existing)
        if _fingerprint(existing_resolution) != _fingerprint(resolution):
            raise PurchasePurposeConflictError(
                "A different purchase-purpose resolution already exists for this review version."
            )
        return existing_resolution

    def _handle_integrity_error(
        self,
        resolution: PurchasePurposeResolution,
        *,
        exc: IntegrityError,
    ) -> PurchasePurposeResolution:
        try:
            existing = self._find(
                review_id=resolution.review_id,
                company_id=resolution.company_id,
                review_version=resolution.review_version,
            )
        except SQLAlchemyError as lookup_exc:
            raise PurchasePurposeError(SAFE_PURCHASE_PURPOSE_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_conflict(existing, resolution)
            except PurchasePurposeConflictError as conflict_exc:
                raise conflict_exc from exc
        raise PurchasePurposeError(SAFE_PURCHASE_PURPOSE_PERSISTENCE_ERROR) from exc
