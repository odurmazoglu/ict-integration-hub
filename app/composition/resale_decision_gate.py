from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.workbench.resale_decision_gate import ResaleDecisionGate
from app.composition.purchase_account_discovery import build_get_product_purchase_account_use_case
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.persistence import SqlAlchemyReviewPurchasePurposeResolutionRepository


def build_resale_decision_gate(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client,
) -> ResaleDecisionGate:
    """Compose the RESALE decision-acceptance gate (P0-PROD-18E-1B).

    Reuses P0-PROD-18D's read-only product purchase-account discovery on the same
    Odoo client as the decision's other read-only lookups; the allowlist comes only
    from ``RESALE_PRODUCT_CATEGORY_IDS``.
    """

    return ResaleDecisionGate(
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        product_account_resolver=build_get_product_purchase_account_use_case(
            settings=settings,
            odoo_client=odoo_client,
        ),
        approved_category_ids=settings.resale_product_category_ids,
    )


__all__ = ["build_resale_decision_gate"]
