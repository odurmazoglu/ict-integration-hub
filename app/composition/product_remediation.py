from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench.product_mapping_use_cases import MapExistingProductUseCase
from app.application.workbench.product_remediation_category import ProductRemediationCategoryPolicy
from app.application.workbench.product_remediation_use_cases import CreateNewProductUseCase
from app.composition.imports import build_deterministic_decision_engine
from app.composition.purchase_account_discovery import (
    build_get_product_purchase_account_use_case,
    build_list_category_purchase_accounts_use_case,
)
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.existing_supplier_info_reader import OdooExistingSupplierInfoReader
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.selected_product_reader import OdooSelectedProductReader
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy
from app.erp.write.odoo_product_writer import OdooProductTemplateRepository, OdooProductWriter
from app.erp.write.odoo_supplierinfo_writer import OdooSupplierInfoRepository, OdooSupplierInfoWriter
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
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


def build_map_existing_product_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> MapExistingProductUseCase:
    """Compose the PRODUCT_NOT_FOUND existing-product mapping (one line -> one existing product).

    The single Odoo write is the existing ``OdooSupplierInfoWriter`` behind the same
    ``OdooProductWritePolicy`` CREATE_NEW_PRODUCT uses; the reclassifier is composed exactly
    like the supplier-remediation one (``MASTER_DATA_CHANGED``, no projection publisher --
    the operator request workflow refreshes the projection after the request).
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    supplierinfo_repository = OdooSupplierInfoRepository(client=resolved_odoo_client)
    review_repository = SqlAlchemyReviewRepository(session)
    source_invoice_reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)
    return MapExistingProductUseCase(
        review_reader=review_repository,
        source_invoice_reader=source_invoice_reader,
        execution_evidence_reader=review_repository,
        product_reader=OdooSelectedProductReader(
            product_repository=OdooProductRepository(adapter=OdooReadOnlyAdapter(client=resolved_odoo_client))
        ),
        identity_claim_reader=SqlAlchemyReviewProductIdentityClaimRepository(session),
        existing_supplier_info_reader=OdooExistingSupplierInfoReader(repository=supplierinfo_repository),
        supplier_info_writer=OdooSupplierInfoWriter(
            repository=supplierinfo_repository, policy=OdooProductWritePolicy.from_settings(settings)
        ),
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=build_deterministic_decision_engine(
                session=session, settings=settings, odoo_client=resolved_odoo_client
            ),
            source_invoice_reader=source_invoice_reader,
            reclassification_writer=review_repository,
            supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
            operating_expense_matcher=OperatingExpenseMatchingEngine(
                SqlAlchemyOperatingExpenseMappingRepository(session)
            ),
            review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        write_authorization_repository=SqlAlchemyWriteAuthorizationRepository(session),
    )
