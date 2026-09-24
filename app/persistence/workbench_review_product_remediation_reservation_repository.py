from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import (
    ProductRemediationConflictError,
    ProductRemediationDataIntegrityError,
    ProductRemediationError,
)
from app.application.workbench.product_remediation import ProductRemediationReservation, ProductReservationStatus
from app.models.workbench_review_product_remediation_reservation import (
    WorkbenchReviewProductRemediationReservation,
)

SAFE_RESERVATION_PERSISTENCE_ERROR = "Product remediation reservation persistence operation failed."


def _reservation_fingerprint(reservation: ProductRemediationReservation) -> tuple[Any, ...]:
    """Identity fields that must agree for a concurrent reservation attempt to be the *same* request.

    Deliberately excludes ``status`` and the Odoo identity fields (those advance over
    the row's lifetime); a retry of the identical operator decision always fingerprints
    the same regardless of how far the state machine has progressed since.
    """

    return (
        reservation.review_id,
        reservation.company_id,
        reservation.review_version,
        reservation.line_number,
        reservation.resolved_supplier_partner_id,
        reservation.seller_item_code,
        reservation.product_name,
        reservation.is_storable,
        (reservation.internal_reference or None),
        (reservation.note or None),
        reservation.categ_id,
    )


def _model_from_reservation(
    reservation: ProductRemediationReservation,
) -> WorkbenchReviewProductRemediationReservation:
    return WorkbenchReviewProductRemediationReservation(
        review_id=reservation.review_id,
        company_id=reservation.company_id,
        review_version=reservation.review_version,
        line_number=reservation.line_number,
        status=reservation.status.value,
        resolved_supplier_partner_id=reservation.resolved_supplier_partner_id,
        seller_item_code=reservation.seller_item_code,
        product_name=reservation.product_name,
        is_storable=reservation.is_storable,
        internal_reference=reservation.internal_reference,
        approved_by=reservation.approved_by,
        note=reservation.note,
        idempotency_key=reservation.idempotency_key,
        product_template_id=reservation.product_template_id,
        product_id=reservation.product_id,
        supplierinfo_id=reservation.supplierinfo_id,
        categ_id=reservation.categ_id,
    )


def _reservation_from_model(
    record: WorkbenchReviewProductRemediationReservation,
) -> ProductRemediationReservation:
    try:
        status = ProductReservationStatus(str(record.status))
    except ValueError as exc:
        raise ProductRemediationDataIntegrityError("Persisted reservation status is not canonical.") from exc
    try:
        return ProductRemediationReservation(
            review_id=str(record.review_id),
            company_id=int(record.company_id),
            review_version=int(record.review_version),
            line_number=str(record.line_number),
            status=status,
            resolved_supplier_partner_id=int(record.resolved_supplier_partner_id),
            seller_item_code=str(record.seller_item_code),
            product_name=str(record.product_name),
            is_storable=bool(record.is_storable),
            internal_reference=record.internal_reference,
            approved_by=record.approved_by,
            note=record.note,
            idempotency_key=record.idempotency_key,
            product_template_id=record.product_template_id,
            product_id=record.product_id,
            supplierinfo_id=record.supplierinfo_id,
            categ_id=record.categ_id,
        )
    except (ProductRemediationError, TypeError, ValueError) as exc:
        raise ProductRemediationDataIntegrityError("Persisted product remediation reservation is invalid.") from exc


class SqlAlchemyReviewProductRemediationReservationRepository:
    """Durable state-machine persistence for one review-line CREATE_NEW_PRODUCT reservation.

    ``reserve`` is the single-winner cross-process barrier for
    ``(review_id, company_id, review_version, line_number)``: a byte-identical
    concurrent retry returns the existing row (harmless race, converges); a
    different decision for the same line fails closed. ``advance`` is a
    compare-and-swap status transition -- it refuses to move the row unless it is
    still exactly at ``expected_status``, so a stray duplicate advancement can never
    silently overwrite already-persisted Odoo identity.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def reserve(self, reservation: ProductRemediationReservation) -> ProductRemediationReservation:
        if not isinstance(reservation, ProductRemediationReservation):
            raise ProductRemediationDataIntegrityError("A canonical ProductRemediationReservation is required.")
        try:
            existing = self._find(
                review_id=reservation.review_id,
                company_id=reservation.company_id,
                review_version=reservation.review_version,
                line_number=reservation.line_number,
            )
            if existing is not None:
                return self._return_existing_or_conflict(existing, reservation)

            record = _model_from_reservation(reservation)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return _reservation_from_model(record)
        except IntegrityError as exc:
            return self._handle_integrity_error(reservation, exc=exc)
        except ProductRemediationError:
            raise
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_RESERVATION_PERSISTENCE_ERROR) from exc

    def find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
        line_number: str,
    ) -> ProductRemediationReservation | None:
        try:
            record = self._find(
                review_id=review_id,
                company_id=company_id,
                review_version=review_version,
                line_number=line_number,
            )
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_RESERVATION_PERSISTENCE_ERROR) from exc
        return _reservation_from_model(record) if record is not None else None

    def advance(
        self,
        reservation: ProductRemediationReservation,
        *,
        expected_status: ProductReservationStatus,
        new_status: ProductReservationStatus,
        product_template_id: int | None = None,
        product_id: int | None = None,
        supplierinfo_id: int | None = None,
    ) -> ProductRemediationReservation:
        try:
            record = self._find(
                review_id=reservation.review_id,
                company_id=reservation.company_id,
                review_version=reservation.review_version,
                line_number=reservation.line_number,
            )
            if record is None:
                raise ProductRemediationDataIntegrityError("The reservation to advance no longer exists.")
            if str(record.status) != expected_status.value:
                raise ProductRemediationDataIntegrityError(
                    "The reservation status changed unexpectedly; refusing to advance."
                )
            record.status = new_status.value
            if product_template_id is not None:
                record.product_template_id = product_template_id
            if product_id is not None:
                record.product_id = product_id
            if supplierinfo_id is not None:
                record.supplierinfo_id = supplierinfo_id
            self._session.flush()
            self._session.refresh(record)
            return _reservation_from_model(record)
        except ProductRemediationError:
            raise
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_RESERVATION_PERSISTENCE_ERROR) from exc

    def _find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
        line_number: str,
    ) -> WorkbenchReviewProductRemediationReservation | None:
        return self._session.scalar(
            select(WorkbenchReviewProductRemediationReservation).where(
                WorkbenchReviewProductRemediationReservation.review_id == review_id,
                WorkbenchReviewProductRemediationReservation.company_id == company_id,
                WorkbenchReviewProductRemediationReservation.review_version == review_version,
                WorkbenchReviewProductRemediationReservation.line_number == line_number,
            )
        )

    def _return_existing_or_conflict(
        self,
        existing: WorkbenchReviewProductRemediationReservation,
        reservation: ProductRemediationReservation,
    ) -> ProductRemediationReservation:
        existing_reservation = _reservation_from_model(existing)
        if _reservation_fingerprint(existing_reservation) != _reservation_fingerprint(reservation):
            raise ProductRemediationConflictError(
                "A different CREATE_NEW_PRODUCT decision already exists for this review line."
            )
        return existing_reservation

    def _handle_integrity_error(
        self,
        reservation: ProductRemediationReservation,
        *,
        exc: IntegrityError,
    ) -> ProductRemediationReservation:
        try:
            existing = self._find(
                review_id=reservation.review_id,
                company_id=reservation.company_id,
                review_version=reservation.review_version,
                line_number=reservation.line_number,
            )
        except SQLAlchemyError as lookup_exc:
            raise ProductRemediationError(SAFE_RESERVATION_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_conflict(existing, reservation)
            except ProductRemediationConflictError as conflict_exc:
                raise conflict_exc from exc
        raise ProductRemediationError(SAFE_RESERVATION_PERSISTENCE_ERROR) from exc
