from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import (
    OneOffVendorRetirementConflictError,
    OneOffVendorRetirementDataIntegrityError,
    OneOffVendorRetirementError,
)
from app.application.workbench.one_off_vendor_retirement import OneOffVendorRetirement, OneOffVendorRetirementStatus
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement

SAFE_RETIREMENT_PERSISTENCE_ERROR = "One-off vendor retirement persistence operation failed."


def _fingerprint(retirement: OneOffVendorRetirement) -> tuple[Any, ...]:
    """Identity fields that must agree for a concurrent create to be the *same* request.

    Deliberately excludes ``status`` -- a retry of the identical creation always
    fingerprints the same regardless of how far the state machine has progressed.
    """

    return (
        retirement.review_id,
        retirement.company_id,
        retirement.review_version,
        retirement.resolved_partner_id,
    )


def _model_from_retirement(retirement: OneOffVendorRetirement) -> WorkbenchReviewOneOffVendorRetirement:
    return WorkbenchReviewOneOffVendorRetirement(
        review_id=retirement.review_id,
        company_id=retirement.company_id,
        review_version=retirement.review_version,
        resolved_partner_id=retirement.resolved_partner_id,
        status=retirement.status.value,
    )


def _retirement_from_model(record: WorkbenchReviewOneOffVendorRetirement) -> OneOffVendorRetirement:
    try:
        status = OneOffVendorRetirementStatus(str(record.status))
    except ValueError as exc:
        raise OneOffVendorRetirementDataIntegrityError("Persisted retirement status is not canonical.") from exc
    try:
        return OneOffVendorRetirement(
            review_id=str(record.review_id),
            company_id=int(record.company_id),
            review_version=int(record.review_version),
            resolved_partner_id=int(record.resolved_partner_id),
            status=status,
        )
    except (OneOffVendorRetirementError, TypeError, ValueError) as exc:
        raise OneOffVendorRetirementDataIntegrityError("Persisted one-off vendor retirement is invalid.") from exc


class SqlAlchemyReviewOneOffVendorRetirementRepository:
    """Durable state-machine persistence for one review's ONE_OFF_VENDOR archive lifecycle.

    ``create`` is the single-winner cross-process barrier for
    ``(review_id, company_id, review_version)``: a byte-identical concurrent
    retry returns the existing row; a different request for the same review
    version fails closed. ``advance`` is a compare-and-swap status transition --
    it refuses to move the row unless it is still exactly at ``expected_status``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_retirement(self, retirement: OneOffVendorRetirement) -> OneOffVendorRetirement:
        if not isinstance(retirement, OneOffVendorRetirement):
            raise OneOffVendorRetirementDataIntegrityError("A canonical OneOffVendorRetirement is required.")
        try:
            existing = self._find(
                review_id=retirement.review_id,
                company_id=retirement.company_id,
                review_version=retirement.review_version,
            )
            if existing is not None:
                return self._return_existing_or_conflict(existing, retirement)

            record = _model_from_retirement(retirement)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return _retirement_from_model(record)
        except IntegrityError as exc:
            return self._handle_integrity_error(retirement, exc=exc)
        except OneOffVendorRetirementError:
            raise
        except SQLAlchemyError as exc:
            raise OneOffVendorRetirementError(SAFE_RETIREMENT_PERSISTENCE_ERROR) from exc

    def find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> OneOffVendorRetirement | None:
        try:
            record = self._find(review_id=review_id, company_id=company_id, review_version=review_version)
        except SQLAlchemyError as exc:
            raise OneOffVendorRetirementError(SAFE_RETIREMENT_PERSISTENCE_ERROR) from exc
        return _retirement_from_model(record) if record is not None else None

    def advance(
        self,
        retirement: OneOffVendorRetirement,
        *,
        expected_status: OneOffVendorRetirementStatus,
        new_status: OneOffVendorRetirementStatus,
    ) -> OneOffVendorRetirement:
        try:
            record = self._find(
                review_id=retirement.review_id,
                company_id=retirement.company_id,
                review_version=retirement.review_version,
            )
            if record is None:
                raise OneOffVendorRetirementDataIntegrityError("The retirement row to advance no longer exists.")
            if str(record.status) != expected_status.value:
                raise OneOffVendorRetirementDataIntegrityError(
                    "The retirement status changed unexpectedly; refusing to advance."
                )
            record.status = new_status.value
            self._session.flush()
            self._session.refresh(record)
            return _retirement_from_model(record)
        except OneOffVendorRetirementError:
            raise
        except SQLAlchemyError as exc:
            raise OneOffVendorRetirementError(SAFE_RETIREMENT_PERSISTENCE_ERROR) from exc

    def _find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchReviewOneOffVendorRetirement | None:
        return self._session.scalar(
            select(WorkbenchReviewOneOffVendorRetirement).where(
                WorkbenchReviewOneOffVendorRetirement.review_id == review_id,
                WorkbenchReviewOneOffVendorRetirement.company_id == company_id,
                WorkbenchReviewOneOffVendorRetirement.review_version == review_version,
            )
        )

    def _return_existing_or_conflict(
        self,
        existing: WorkbenchReviewOneOffVendorRetirement,
        retirement: OneOffVendorRetirement,
    ) -> OneOffVendorRetirement:
        existing_retirement = _retirement_from_model(existing)
        if _fingerprint(existing_retirement) != _fingerprint(retirement):
            raise OneOffVendorRetirementConflictError(
                "A different one-off vendor retirement already exists for this review version."
            )
        return existing_retirement

    def _handle_integrity_error(
        self,
        retirement: OneOffVendorRetirement,
        *,
        exc: IntegrityError,
    ) -> OneOffVendorRetirement:
        try:
            existing = self._find(
                review_id=retirement.review_id,
                company_id=retirement.company_id,
                review_version=retirement.review_version,
            )
        except SQLAlchemyError as lookup_exc:
            raise OneOffVendorRetirementError(SAFE_RETIREMENT_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_conflict(existing, retirement)
            except OneOffVendorRetirementConflictError as conflict_exc:
                raise conflict_exc from exc
        raise OneOffVendorRetirementError(SAFE_RETIREMENT_PERSISTENCE_ERROR) from exc
