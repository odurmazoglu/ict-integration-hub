from __future__ import annotations

from app.application.workbench.purchase_account_discovery_use_cases import (
    GetProductPurchaseAccountUseCase,
    ListCategoryPurchaseAccountsUseCase,
)
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.purchase_account_discovery_reader import OdooPurchaseAccountDiscoveryReader


def build_purchase_account_discovery_reader(
    *,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> OdooPurchaseAccountDiscoveryReader:
    """Compose the read-only purchase-account discovery reader (P0-PROD-18D)."""

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    return OdooPurchaseAccountDiscoveryReader(adapter=OdooReadOnlyAdapter(client=resolved_odoo_client))


def build_list_category_purchase_accounts_use_case(
    *,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> ListCategoryPurchaseAccountsUseCase:
    return ListCategoryPurchaseAccountsUseCase(
        reader=build_purchase_account_discovery_reader(settings=settings, odoo_client=odoo_client)
    )


def build_get_product_purchase_account_use_case(
    *,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> GetProductPurchaseAccountUseCase:
    return GetProductPurchaseAccountUseCase(
        reader=build_purchase_account_discovery_reader(settings=settings, odoo_client=odoo_client)
    )


__all__ = [
    "build_get_product_purchase_account_use_case",
    "build_list_category_purchase_accounts_use_case",
    "build_purchase_account_discovery_reader",
]
