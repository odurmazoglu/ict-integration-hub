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
)
from app.application.workbench.supplier_remediation import (
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
)
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect

SAFE_REMEDIATION_EFFECT_PERSISTENCE_ERROR = "Supplier remediation effect persistence operation failed."


def remediation_effect_fingerprint(effect: SupplierRemediationEffect) -> tuple[Any, ...]:
    return (
        effect.mode.value,
        effect.review_id,
        effect.company_id,
        effect.review_version,
        effect.source_invoice_id,
        effect.resolved_partner_id,
        effect.partner_write_status.value,
        (effect.source_supplier_tax_number or None),
        (effect.approved_by or None),
    )


def model_from_remediation_effect(effect: SupplierRemediationEffect) -> WorkbenchReviewSupplierRemediationEffect:
    return WorkbenchReviewSupplierRemediationEffect(
        review_id=effect.review_id,
        company_id=effect.company_id,
        review_version=effect.review_version,
        source_invoice_id=effect.source_invoice_id,
        mode=effect.mode.value,
        resolved_partner_id=effect.resolved_partner_id,
        partner_write_status=effect.partner_write_status.value,
        source_supplier_tax_number=effect.source_supplier_tax_number,
        approved_by=effect.approved_by,
    )


def remediation_effect_from_model(record: WorkbenchReviewSupplierRemediationEffect) -> SupplierRemediationEffect:
    try:
        mode = SupplierResolutionMode(str(record.mode))
        write_status = SupplierPartnerWriteEffectStatus(str(record.partner_write_status))
    except ValueError as exc:
        raise SupplierResolutionDataIntegrityError("Persisted supplier remediation effect is not canonical.") from exc
    try:
        return SupplierRemediationEffect(
            review_id=str(record.review_id),
            company_id=int(record.company_id),
            review_version=int(record.review_version),
            source_invoice_id=str(record.source_invoice_id),
            mode=mode,
            resolved_partner_id=int(record.resolved_partner_id),
            partner_write_status=write_status,
            source_supplier_tax_number=record.source_supplier_tax_number,
            approved_by=record.approved_by,
        )
    except (SupplierResolutionContractError, TypeError, ValueError) as exc:
        raise SupplierResolutionDataIntegrityError("Persisted supplier remediation effect is invalid.") from exc


class SqlAlchemyReviewSupplierRemediationEffectRepository:
    """Append-only persistence for the completed effect of a supplier remediation.

    One effect per ``(review_id, review_version)``. Byte-identical retry returns
    the existing row; a different effect for the same review version fails closed.
    No UPDATE path.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_remediation_effect(self, effect: SupplierRemediationEffect) -> SupplierRemediationEffect:
        if not isinstance(effect, SupplierRemediationEffect):
            raise SupplierResolutionContractError("A canonical SupplierRemediationEffect is required.")
        try:
            existing = self._find(
                review_id=effect.review_id,
                company_id=effect.company_id,
                review_version=effect.review_version,
            )
            if existing is not None:
                return self._return_existing_or_conflict(existing, effect)

            record = model_from_remediation_effect(effect)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return remediation_effect_from_model(record)
        except IntegrityError as exc:
            return self._handle_integrity_error(effect, exc=exc)
        except SupplierResolutionError:
            raise
        except SQLAlchemyError as exc:
            raise SupplierResolutionError(SAFE_REMEDIATION_EFFECT_PERSISTENCE_ERROR) from exc

    def find_remediation_effect(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> SupplierRemediationEffect | None:
        if not isinstance(review_id, str) or not review_id.strip():
            raise SupplierResolutionContractError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise SupplierResolutionContractError("company_id must be positive.")
        if type(review_version) is not int or review_version <= 0:
            raise SupplierResolutionContractError("review_version must be positive.")
        try:
            record = self._find(review_id=review_id, company_id=company_id, review_version=review_version)
        except SQLAlchemyError as exc:
            raise SupplierResolutionError(SAFE_REMEDIATION_EFFECT_PERSISTENCE_ERROR) from exc
        return remediation_effect_from_model(record) if record is not None else None

    def _find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchReviewSupplierRemediationEffect | None:
        return self._session.scalar(
            select(WorkbenchReviewSupplierRemediationEffect).where(
                WorkbenchReviewSupplierRemediationEffect.review_id == review_id,
                WorkbenchReviewSupplierRemediationEffect.company_id == company_id,
                WorkbenchReviewSupplierRemediationEffect.review_version == review_version,
            )
        )

    def _return_existing_or_conflict(
        self,
        existing: WorkbenchReviewSupplierRemediationEffect,
        effect: SupplierRemediationEffect,
    ) -> SupplierRemediationEffect:
        existing_effect = remediation_effect_from_model(existing)
        if remediation_effect_fingerprint(existing_effect) != remediation_effect_fingerprint(effect):
            raise SupplierResolutionConflictError(
                "A different supplier remediation effect already exists for this review version."
            )
        return existing_effect

    def _handle_integrity_error(
        self,
        effect: SupplierRemediationEffect,
        *,
        exc: IntegrityError,
    ) -> SupplierRemediationEffect:
        try:
            existing = self._find(
                review_id=effect.review_id,
                company_id=effect.company_id,
                review_version=effect.review_version,
            )
        except SQLAlchemyError as lookup_exc:
            raise SupplierResolutionError(SAFE_REMEDIATION_EFFECT_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_conflict(existing, effect)
            except SupplierResolutionConflictError as conflict_exc:
                raise conflict_exc from exc
        raise SupplierResolutionError(SAFE_REMEDIATION_EFFECT_PERSISTENCE_ERROR) from exc
