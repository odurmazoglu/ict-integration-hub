from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import (
    SupplierResolutionConflictError,
    SupplierResolutionContractError,
    SupplierResolutionDataIntegrityError,
    SupplierResolutionError,
    SupplierResolutionNotFoundError,
)
from app.application.workbench.supplier_resolution import SupplierResolution, SupplierResolutionMode
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution

SAFE_SUPPLIER_RESOLUTION_PERSISTENCE_ERROR = "Supplier resolution persistence operation failed."


def supplier_resolution_fingerprint(resolution: SupplierResolution) -> tuple[Any, ...]:
    return (
        resolution.mode.value,
        resolution.review_id,
        resolution.company_id,
        resolution.review_version,
        resolution.source_invoice_id,
        resolution.resolved_partner_id,
        (resolution.approved_by or None),
        (resolution.note or None),
    )


def model_from_supplier_resolution(resolution: SupplierResolution) -> WorkbenchReviewSupplierResolution:
    return WorkbenchReviewSupplierResolution(
        review_id=resolution.review_id,
        company_id=resolution.company_id,
        review_version=resolution.review_version,
        source_invoice_id=resolution.source_invoice_id,
        mode=resolution.mode.value,
        resolved_partner_id=resolution.resolved_partner_id,
        approved_by=resolution.approved_by,
        note=resolution.note,
    )


def supplier_resolution_from_model(record: WorkbenchReviewSupplierResolution) -> SupplierResolution:
    try:
        mode = SupplierResolutionMode(str(record.mode))
    except ValueError as exc:
        raise SupplierResolutionDataIntegrityError("Persisted supplier resolution mode is not canonical.") from exc
    try:
        return SupplierResolution(
            mode=mode,
            review_id=str(record.review_id),
            company_id=int(record.company_id),
            review_version=int(record.review_version),
            source_invoice_id=str(record.source_invoice_id),
            resolved_partner_id=int(record.resolved_partner_id) if record.resolved_partner_id is not None else None,
            approved_by=record.approved_by,
            note=record.note,
        )
    except (SupplierResolutionContractError, TypeError, ValueError) as exc:
        raise SupplierResolutionDataIntegrityError("Persisted supplier resolution is invalid.") from exc


class SqlAlchemyReviewSupplierResolutionRepository:
    """Append-only persistence for explicit supplier-resolution decisions.

    One resolution per ``(review_id, review_version)``. A byte-identical retry
    returns the existing row; a different decision for the same review version
    fails closed. There is no UPDATE path.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_supplier_resolution(self, resolution: SupplierResolution) -> SupplierResolution:
        if not isinstance(resolution, SupplierResolution):
            raise SupplierResolutionContractError("A canonical SupplierResolution is required.")
        try:
            existing = self._find(
                review_id=resolution.review_id,
                company_id=resolution.company_id,
                review_version=resolution.review_version,
            )
            if existing is not None:
                return self._return_existing_or_conflict(existing, resolution)

            record = model_from_supplier_resolution(resolution)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return supplier_resolution_from_model(record)
        except IntegrityError as exc:
            return self._handle_integrity_error(resolution, exc=exc)
        except SupplierResolutionError:
            raise
        except SQLAlchemyError as exc:
            raise SupplierResolutionError(SAFE_SUPPLIER_RESOLUTION_PERSISTENCE_ERROR) from exc

    def get_supplier_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> SupplierResolution:
        if not isinstance(review_id, str) or not review_id.strip():
            raise SupplierResolutionContractError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise SupplierResolutionContractError("company_id must be positive.")
        if type(review_version) is not int or review_version <= 0:
            raise SupplierResolutionContractError("review_version must be positive.")
        try:
            record = self._find(review_id=review_id, company_id=company_id, review_version=review_version)
        except SQLAlchemyError as exc:
            raise SupplierResolutionError(SAFE_SUPPLIER_RESOLUTION_PERSISTENCE_ERROR) from exc
        if record is None:
            raise SupplierResolutionNotFoundError("No supplier resolution exists for this review version.")
        return supplier_resolution_from_model(record)

    def _find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchReviewSupplierResolution | None:
        return self._session.scalar(
            select(WorkbenchReviewSupplierResolution).where(
                WorkbenchReviewSupplierResolution.review_id == review_id,
                WorkbenchReviewSupplierResolution.company_id == company_id,
                WorkbenchReviewSupplierResolution.review_version == review_version,
            )
        )

    def _return_existing_or_conflict(
        self,
        existing: WorkbenchReviewSupplierResolution,
        resolution: SupplierResolution,
    ) -> SupplierResolution:
        existing_resolution = supplier_resolution_from_model(existing)
        if supplier_resolution_fingerprint(existing_resolution) != supplier_resolution_fingerprint(resolution):
            raise SupplierResolutionConflictError(
                "A different supplier resolution already exists for this review version."
            )
        return existing_resolution

    def _handle_integrity_error(
        self,
        resolution: SupplierResolution,
        *,
        exc: IntegrityError,
    ) -> SupplierResolution:
        try:
            existing = self._find(
                review_id=resolution.review_id,
                company_id=resolution.company_id,
                review_version=resolution.review_version,
            )
        except SQLAlchemyError as lookup_exc:
            raise SupplierResolutionError(SAFE_SUPPLIER_RESOLUTION_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_conflict(existing, resolution)
            except SupplierResolutionConflictError as conflict_exc:
                raise conflict_exc from exc
        raise SupplierResolutionError(SAFE_SUPPLIER_RESOLUTION_PERSISTENCE_ERROR) from exc
