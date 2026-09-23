from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.accounting_resolution import AccountingTreatmentType, ReviewAccountingResolution
from app.application.workbench.exceptions import (
    AccountingResolutionConflictError,
    AccountingResolutionError,
    WorkbenchContractError,
)
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution

SAFE_ACCOUNTING_RESOLUTION_PERSISTENCE_ERROR = "Accounting resolution persistence operation failed."


def _fingerprint(resolution: ReviewAccountingResolution) -> tuple[Any, ...]:
    return (
        resolution.review_id,
        resolution.company_id,
        resolution.review_version,
        resolution.treatment_type.value,
        resolution.expense_account_id,
        resolution.expense_category,
        (resolution.approved_by or None),
        (resolution.note or None),
    )


def _model_from_resolution(resolution: ReviewAccountingResolution) -> WorkbenchReviewAccountingResolution:
    return WorkbenchReviewAccountingResolution(
        review_id=resolution.review_id,
        company_id=resolution.company_id,
        review_version=resolution.review_version,
        treatment_type=resolution.treatment_type.value,
        expense_account_id=resolution.expense_account_id,
        expense_category=resolution.expense_category,
        approved_by=resolution.approved_by,
        note=resolution.note,
    )


def _resolution_from_model(record: WorkbenchReviewAccountingResolution) -> ReviewAccountingResolution:
    try:
        treatment_type = AccountingTreatmentType(str(record.treatment_type))
    except ValueError as exc:
        raise WorkbenchContractError("Persisted accounting resolution is not canonical.") from exc
    return ReviewAccountingResolution(
        id=int(record.id),
        review_id=str(record.review_id),
        company_id=int(record.company_id),
        review_version=int(record.review_version),
        treatment_type=treatment_type,
        expense_account_id=int(record.expense_account_id),
        expense_category=str(record.expense_category),
        approved_by=record.approved_by,
        note=record.note,
    )


class SqlAlchemyReviewAccountingResolutionRepository:
    """Append-only persistence for a review-scoped accounting resolution.

    One resolution per ``(review_id, review_version)``. A byte-identical retry
    returns the existing row; a different resolution for the same review version
    fails closed. There is no UPDATE path. Never writes to the supplier-wide
    ``operating_expense_mappings`` table.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_accounting_resolution(self, resolution: ReviewAccountingResolution) -> ReviewAccountingResolution:
        if not isinstance(resolution, ReviewAccountingResolution):
            raise WorkbenchContractError("A canonical ReviewAccountingResolution is required.")
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
        except AccountingResolutionError:
            raise
        except SQLAlchemyError as exc:
            raise AccountingResolutionError(SAFE_ACCOUNTING_RESOLUTION_PERSISTENCE_ERROR) from exc

    def find_accounting_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewAccountingResolution | None:
        if not isinstance(review_id, str) or not review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        if type(review_version) is not int or review_version <= 0:
            raise WorkbenchContractError("review_version must be positive.")
        try:
            record = self._find(review_id=review_id, company_id=company_id, review_version=review_version)
        except SQLAlchemyError as exc:
            raise AccountingResolutionError(SAFE_ACCOUNTING_RESOLUTION_PERSISTENCE_ERROR) from exc
        return _resolution_from_model(record) if record is not None else None

    def find_latest_accounting_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> ReviewAccountingResolution | None:
        """The most recently recorded resolution for this review, regardless of exact
        version -- mirrors ``find_latest_remediation_effect`` exactly; a review has at
        most one effective accounting resolution for its lifetime.
        """

        if not isinstance(review_id, str) or not review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        try:
            record = self._session.scalar(
                select(WorkbenchReviewAccountingResolution)
                .where(
                    WorkbenchReviewAccountingResolution.review_id == review_id,
                    WorkbenchReviewAccountingResolution.company_id == company_id,
                )
                .order_by(WorkbenchReviewAccountingResolution.review_version.desc())
                .limit(1)
            )
        except SQLAlchemyError as exc:
            raise AccountingResolutionError(SAFE_ACCOUNTING_RESOLUTION_PERSISTENCE_ERROR) from exc
        return _resolution_from_model(record) if record is not None else None

    def _find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchReviewAccountingResolution | None:
        return self._session.scalar(
            select(WorkbenchReviewAccountingResolution).where(
                WorkbenchReviewAccountingResolution.review_id == review_id,
                WorkbenchReviewAccountingResolution.company_id == company_id,
                WorkbenchReviewAccountingResolution.review_version == review_version,
            )
        )

    def _return_existing_or_conflict(
        self,
        existing: WorkbenchReviewAccountingResolution,
        resolution: ReviewAccountingResolution,
    ) -> ReviewAccountingResolution:
        existing_resolution = _resolution_from_model(existing)
        if _fingerprint(existing_resolution) != _fingerprint(resolution):
            raise AccountingResolutionConflictError(
                "A different accounting resolution already exists for this review version."
            )
        return existing_resolution

    def _handle_integrity_error(
        self,
        resolution: ReviewAccountingResolution,
        *,
        exc: IntegrityError,
    ) -> ReviewAccountingResolution:
        try:
            existing = self._find(
                review_id=resolution.review_id,
                company_id=resolution.company_id,
                review_version=resolution.review_version,
            )
        except SQLAlchemyError as lookup_exc:
            raise AccountingResolutionError(SAFE_ACCOUNTING_RESOLUTION_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_conflict(existing, resolution)
            except AccountingResolutionConflictError as conflict_exc:
                raise conflict_exc from exc
        raise AccountingResolutionError(SAFE_ACCOUNTING_RESOLUTION_PERSISTENCE_ERROR) from exc
