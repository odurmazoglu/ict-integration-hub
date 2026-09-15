from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import ProductRemediationError, ProductRemediationRaceError
from app.application.workbench.product_remediation import ProductIdentityClaim
from app.models.workbench_review_product_identity_claim import WorkbenchReviewProductIdentityClaim

SAFE_IDENTITY_CLAIM_PERSISTENCE_ERROR = "Product identity claim persistence operation failed."


def _model_from_claim(claim: ProductIdentityClaim) -> WorkbenchReviewProductIdentityClaim:
    return WorkbenchReviewProductIdentityClaim(
        company_id=claim.company_id,
        resolved_supplier_partner_id=claim.resolved_supplier_partner_id,
        seller_item_code=claim.seller_item_code,
        owner_review_id=claim.owner_review_id,
        owner_company_id=claim.owner_company_id,
        owner_review_version=claim.owner_review_version,
        owner_line_number=claim.owner_line_number,
    )


def _claim_from_model(record: WorkbenchReviewProductIdentityClaim) -> ProductIdentityClaim:
    return ProductIdentityClaim(
        company_id=int(record.company_id),
        resolved_supplier_partner_id=int(record.resolved_supplier_partner_id),
        seller_item_code=str(record.seller_item_code),
        owner_review_id=str(record.owner_review_id),
        owner_company_id=int(record.owner_company_id),
        owner_review_version=int(record.owner_review_version),
        owner_line_number=str(record.owner_line_number),
    )


class SqlAlchemyReviewProductIdentityClaimRepository:
    """Durable cross-review lock for one supplier-product identity.

    ``claim`` is the single-winner barrier for
    ``(company_id, resolved_supplier_partner_id, seller_item_code)``. Unlike the
    reservation repository's ``reserve``, a concurrent-INSERT loser here is always
    a genuinely *different* review line (the caller only ever reaches ``claim``
    once per fresh reservation, after its own ``find`` on the reservation table
    already returned nothing) -- so every collision is raised out as
    :class:`ProductRemediationRaceError`, never silently returned. The caller
    resolves the race by reading the winner's reservation via ``find`` and either
    reusing its resolved product or failing closed/retryable if it is not resolved yet.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim(self, claim: ProductIdentityClaim) -> ProductIdentityClaim:
        if not isinstance(claim, ProductIdentityClaim):
            raise ProductRemediationError(SAFE_IDENTITY_CLAIM_PERSISTENCE_ERROR)
        try:
            record = _model_from_claim(claim)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return _claim_from_model(record)
        except IntegrityError as exc:
            raise ProductRemediationRaceError(
                "Another request is already creating a product for this exact supplier/item identity."
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_IDENTITY_CLAIM_PERSISTENCE_ERROR) from exc

    def find(
        self,
        *,
        company_id: int,
        resolved_supplier_partner_id: int,
        seller_item_code: str,
    ) -> ProductIdentityClaim | None:
        try:
            record = self._session.scalar(
                select(WorkbenchReviewProductIdentityClaim).where(
                    WorkbenchReviewProductIdentityClaim.company_id == company_id,
                    WorkbenchReviewProductIdentityClaim.resolved_supplier_partner_id == resolved_supplier_partner_id,
                    WorkbenchReviewProductIdentityClaim.seller_item_code == seller_item_code,
                )
            )
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_IDENTITY_CLAIM_PERSISTENCE_ERROR) from exc
        return _claim_from_model(record) if record is not None else None
