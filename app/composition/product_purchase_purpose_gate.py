from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.workbench.product_purchase_purpose_gate import ProductPurchasePurposeGate
from app.persistence import SqlAlchemyReviewPurchasePurposeResolutionRepository


def build_product_purchase_purpose_gate(*, session: Session) -> ProductPurchasePurposeGate:
    """Compose the product-coded Vendor Bill purpose requirement: Hub purpose rows only, no Odoo read."""

    return ProductPurchasePurposeGate(purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session))


__all__ = ["build_product_purchase_purpose_gate"]
