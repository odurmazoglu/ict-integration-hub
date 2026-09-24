from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.workbench.product_remediation_category import ProductRemediationCategoryPolicy
from app.application.workbench.product_remediation_use_cases import CreateNewProductUseCase
from app.composition.purchase_account_discovery import (
    build_get_product_purchase_account_use_case,
    build_list_category_purchase_accounts_use_case,
)
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.existing_supplier_info_reader import OdooExistingSupplierInfoReader
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy
from app.erp.write.odoo_product_writer import OdooProductTemplateRepository, OdooProductWriter
from app.erp.write.odoo_supplierinfo_writer import OdooSupplierInfoRepository, OdooSupplierInfoWriter
from app.persistence import (
    SqlAlchemyReviewProductIdentityClaimRepository,
    SqlAlchemyReviewProductRemediationReservationRepository,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyWriteAuthorizationRepository,
)


def build_create_new_product_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> CreateNewProductUseCase:
    """Compose the CREATE_NEW_PRODUCT remediation orchestration (P0-PROD-07G).

    Reuses the PR #140 narrow Odoo write infrastructure exactly as designed: both
    ``OdooProductWriter`` and ``OdooSupplierInfoWriter`` share the single
    ``OdooProductWritePolicy`` (``PRODUCT_REMEDIATION_WRITE_ENABLED``, default
    ``False``). The supplierinfo *read* path reuses the same
    ``OdooSupplierInfoRepository`` the writer already uses for its own
    read-before-write check -- no new Odoo model access.

    The optional category (P0-PROD-18E-2) is validated and re-verified through
    P0-PROD-18D's read-only purchase-account discovery on the same Odoo client; the
    RESALE allowlist comes only from ``RESALE_PRODUCT_CATEGORY_IDS``.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    policy = OdooProductWritePolicy.from_settings(settings)
    supplierinfo_repository = OdooSupplierInfoRepository(client=resolved_odoo_client)

    return CreateNewProductUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        reservation_writer=SqlAlchemyReviewProductRemediationReservationRepository(session),
        identity_claim_writer=SqlAlchemyReviewProductIdentityClaimRepository(session),
        existing_supplier_info_reader=OdooExistingSupplierInfoReader(repository=supplierinfo_repository),
        product_writer=OdooProductWriter(
            repository=OdooProductTemplateRepository(client=resolved_odoo_client),
            policy=policy,
        ),
        supplier_info_writer=OdooSupplierInfoWriter(
            repository=supplierinfo_repository,
            policy=policy,
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        write_authorization_repository=SqlAlchemyWriteAuthorizationRepository(session),
        category_policy=ProductRemediationCategoryPolicy(
            purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
            category_lister=build_list_category_purchase_accounts_use_case(
                settings=settings, odoo_client=resolved_odoo_client
            ),
            product_account_resolver=build_get_product_purchase_account_use_case(
                settings=settings, odoo_client=resolved_odoo_client
            ),
            approved_category_ids=settings.resale_product_category_ids,
        ),
    )
