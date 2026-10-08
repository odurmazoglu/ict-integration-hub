"""``MapExistingProductUseCase`` -- see ``app.application.workbench.product_mapping``.

Order (every check before any write; the only Odoo write is one product.supplierinfo):

1. review pending at exactly ``expected_version``; the line carries PRODUCT_NOT_FOUND;
2. seller product code from the immutable source line (none -> refused, never invented);
3. supplier = the review's effective supplier (``EffectiveSupplierResolver``): the raw
   deterministic match, else a proven accepted supplier resolution -- the exact partner
   the follow-up reclassification matches products under;
4. selected product: exists, active, company-compatible, has a template (read-only);
5. no CREATE_NEW_PRODUCT identity claim and no supplierinfo mapping this supplier/code
   to a different product (fail closed); an identical existing mapping is reused;
6. narrow MAP_EXISTING_PRODUCT authorization consumed, supplierinfo created through the
   existing guarded writer (read-before-write, race-detecting);
7. MASTER_DATA_CHANGED reclassification, committed together with the consumption.

Any failure rolls the Hub transaction back (the authorization stays unconsumed). A
supplierinfo created before a later failure is found again by step 5 on retry, so a
double-click or resume never creates a second mapping.
"""

from __future__ import annotations

from typing import Protocol

from app.application.commands.product_remediation import CreateSupplierInfoCommand
from app.application.dto.product_remediation import SupplierInfoWriteStatus
from app.application.effective_supplier import EffectiveSupplier, EffectiveSupplierResolverPort
from app.application.ports.existing_supplier_info_reader import ExistingSupplierInfoReader
from app.application.ports.supplier_info_writer import SupplierInfoWriter
from app.application.services import UnitOfWork
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    ProductRemediationContractError,
    ProductRemediationEligibilityError,
    ProductRemediationSupplierUnresolvedError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
)
from app.application.workbench.ports import (
    ProductIdentityClaimWriter,
    ReviewQueueReader,
    ReviewSourceInvoiceEvidenceReader,
    SelectedProductReader,
)
from app.application.workbench.product_mapping import (
    MapExistingProductCommand,
    MapExistingProductResult,
    ProductMappingConflictError,
    ProductMappingProductInvalidError,
    ProductMappingSellerCodeMissingError,
)
from app.application.workbench.product_remediation import normalize_seller_item_code
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import (
    ReclassifyReviewCommand,
    ReviewReclassificationResult,
    ReviewReclassificationTrigger,
)
from app.application.workbench.selected_product_resolution import ResolutionProductRecord
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRepository,
    product_mapping_authorization_consumer_id,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode
from app.domain.invoice import InternalInvoice

STALE_MESSAGE = "İnceleme bu istekten sonra değişti; güncel kaydı kontrol edip tekrar gönderin."


class Reclassifier(Protocol):
    async def execute(self, command: ReclassifyReviewCommand) -> ReviewReclassificationResult: ...


class MapExistingProductUseCase:
    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        effective_supplier_resolver: EffectiveSupplierResolverPort,
        product_reader: SelectedProductReader,
        identity_claim_reader: ProductIdentityClaimWriter,
        existing_supplier_info_reader: ExistingSupplierInfoReader,
        supplier_info_writer: SupplierInfoWriter,
        reclassifier: Reclassifier,
        unit_of_work: UnitOfWork,
        write_authorization_repository: WriteAuthorizationRepository | None = None,
    ) -> None:
        self._review_reader = review_reader
        self._source_invoice_reader = source_invoice_reader
        self._effective_supplier_resolver = effective_supplier_resolver
        self._product_reader = product_reader
        self._identity_claim_reader = identity_claim_reader
        self._existing_supplier_info_reader = existing_supplier_info_reader
        self._supplier_info_writer = supplier_info_writer
        self._reclassifier = reclassifier
        self._unit_of_work = unit_of_work
        self._write_authorization_repository = write_authorization_repository

    async def execute(self, command: MapExistingProductCommand) -> MapExistingProductResult:
        if not isinstance(command, MapExistingProductCommand):
            raise ProductRemediationContractError("A canonical MapExistingProductCommand is required.")
        line_number = command.line_number.strip()
        self._require_eligible_review(command, line_number)
        source = self._source_invoice_reader.get(review_id=command.review_id, company_id=command.company_id)
        line = _source_line(source, line_number)
        seller_item_code = normalize_seller_item_code(line.seller_item_code)
        if seller_item_code is None:
            raise ProductMappingSellerCodeMissingError(
                f"Satır {line_number} için faturada satıcı ürün kodu yok; tedarikçiye özel ürün eşleştirmesi "
                "yapılamaz. Teknik destek alın."
            )
        supplier = self._effective_supplier(command, source.invoice)
        partner_id = supplier.partner_id
        product = self._selected_product(command)
        self._require_no_create_in_flight(command, partner_id, seller_item_code)
        existing_id = await self._existing_identical_mapping(command, partner_id, seller_item_code, product)

        try:
            supplierinfo_id, created = existing_id, False
            if existing_id is None:
                supplierinfo_id, created = await self._create_mapping(
                    command, line_number, partner_id, seller_item_code, product, line.description
                )
            reclass = await self._reclassifier.execute(
                ReclassifyReviewCommand(
                    review_id=command.review_id,
                    company_id=command.company_id,
                    expected_version=command.expected_version,
                    trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
                    note=_note(command, line_number, seller_item_code, supplier, product, supplierinfo_id),
                )
            )
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise

        remaining = _product_not_found_lines(reclass.new_review_reasons)
        return MapExistingProductResult(
            review_id=command.review_id,
            company_id=command.company_id,
            line_number=line_number,
            seller_item_code=seller_item_code,
            supplier_partner_id=partner_id,
            product_id=product.id,
            product_name=product.name,
            supplierinfo_id=supplierinfo_id,
            created_supplierinfo=created,
            previous_version=reclass.from_version,
            current_version=reclass.to_version,
            line_resolved=line_number not in remaining,
            remaining_product_lines=remaining,
        )

    # ------------------------------------------------------------------ checks

    def _require_eligible_review(self, command: MapExistingProductCommand, line_number: str) -> None:
        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )
        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewStateConflictError(STALE_MESSAGE)
        if review.version != command.expected_version:
            raise ReviewVersionConflictError(STALE_MESSAGE)
        if line_number not in _product_not_found_lines(review.review_reasons):
            raise ProductRemediationEligibilityError(
                f"Satır {line_number} için ürün eşleştirmesi gerekmiyor (bu satırda 'Ürün Odoo'da bulunamadı' "
                "engeli yok)."
            )

    def _effective_supplier(self, command: MapExistingProductCommand, invoice: InternalInvoice) -> EffectiveSupplier:
        resolution = self._effective_supplier_resolver.resolve(
            review_id=command.review_id, company_id=command.company_id, invoice=invoice
        )
        if resolution.supplier is None:
            raise ProductRemediationSupplierUnresolvedError(resolution.failure or _SUPPLIER_UNRESOLVED)
        return resolution.supplier

    def _selected_product(self, command: MapExistingProductCommand) -> ResolutionProductRecord:
        products = [
            p for p in self._product_reader.find_products_by_ids((command.product_id,)) if p.id == command.product_id
        ]
        product = products[0] if len(products) == 1 else None
        if product is None or not product.active:
            raise ProductMappingProductInvalidError("Seçilen Odoo ürünü bulunamadı veya arşivlenmiş.")
        if product.company_id not in (None, command.company_id):
            raise ProductMappingProductInvalidError("Seçilen Odoo ürünü başka bir şirkete ait.")
        if type(product.product_tmpl_id) is not int or product.product_tmpl_id <= 0:
            raise ProductMappingProductInvalidError("Seçilen Odoo ürününün ürün şablonu okunamadı.")
        return product

    def _require_no_create_in_flight(self, command: MapExistingProductCommand, partner_id: int, code: str) -> None:
        claim = self._identity_claim_reader.find(
            company_id=command.company_id, resolved_supplier_partner_id=partner_id, seller_item_code=code
        )
        if claim is not None:
            raise ProductMappingConflictError(
                f"'{code}' satıcı ürün kodu için yeni ürün oluşturma işlemi zaten kayıtlı; eşleştirme yapılmadı. "
                "Teknik destek alın."
            )

    async def _existing_identical_mapping(
        self, command: MapExistingProductCommand, partner_id: int, code: str, product: ResolutionProductRecord
    ) -> int | None:
        existing = await self._existing_supplier_info_reader.find_existing(
            partner_id=partner_id, product_code=code, company_id=command.company_id
        )
        if not existing:
            return None
        if len(existing) > 1:
            raise ProductMappingConflictError(
                f"Bu tedarikçinin '{code}' ürün kodu için Odoo'da birden fazla tedarikçi ürün kaydı var "
                f"({_ids(existing)}). Hiçbir kayıt değiştirilmedi; Odoo'daki kayıtları kontrol edin."
            )
        row = existing[0]
        same_product = row.product_tmpl_id == product.product_tmpl_id and row.product_id in (None, product.id)
        if not same_product:
            raise ProductMappingConflictError(
                f"Bu tedarikçinin '{code}' ürün kodu Odoo'da zaten başka bir ürüne eşlenmiş (tedarikçi ürün kaydı "
                f"#{row.id}). Mevcut eşleştirme değiştirilmedi; doğru ürün bu değilse Odoo'daki kaydı kontrol edin."
            )
        return row.id

    # ------------------------------------------------------------------ write

    async def _create_mapping(
        self,
        command: MapExistingProductCommand,
        line_number: str,
        partner_id: int,
        code: str,
        product: ResolutionProductRecord,
        description: str | None,
    ) -> tuple[int, bool]:
        assert product.product_tmpl_id is not None  # guaranteed by _selected_product
        result = await self._supplier_info_writer.create_supplier_info(
            CreateSupplierInfoCommand(
                company_id=command.company_id,
                partner_id=partner_id,
                product_tmpl_id=product.product_tmpl_id,
                product_id=product.id,
                product_code=code,
                product_name=(description or "").strip() or None,
                idempotency_key=f"product-mapping:{command.company_id}:{command.review_id}:"
                f"{command.expected_version}:{line_number}",
                approved_by=command.approved_by,
                authorization=self._claim_authorization(command, line_number),
            )
        )
        if result.product_id not in (None, product.id):
            # The writer's own reuse check compares templates only; a concurrently created row
            # pinning another variant of the same template must not be reported as this mapping.
            raise ProductMappingConflictError(
                f"Bu tedarikçinin '{code}' ürün kodu Odoo'da aynı ürünün başka bir varyantına eşlenmiş (tedarikçi "
                f"ürün kaydı #{result.supplierinfo_id}). Mevcut eşleştirme değiştirilmedi."
            )
        return result.supplierinfo_id, result.status is SupplierInfoWriteStatus.CREATED

    def _claim_authorization(self, command: MapExistingProductCommand, line_number: str):
        if command.authorization_id is None:
            return None
        if self._write_authorization_repository is None:
            raise ProductRemediationContractError("Runtime authorization is not supported by this workflow.")
        return self._write_authorization_repository.claim_and_consume(
            company_id=command.company_id,
            review_id=command.review_id,
            operation_type=WriteAuthorizationOperationType.MAP_EXISTING_PRODUCT,
            target_version=command.expected_version,
            authorization_id=command.authorization_id,
            execution_id=product_mapping_authorization_consumer_id(
                company_id=command.company_id,
                review_id=command.review_id,
                expected_version=command.expected_version,
                line_number=line_number,
            ),
        )


_SUPPLIER_UNRESOLVED = "Bu incelemede tedarikçi henüz kesin olarak eşleşmedi; önce tedarikçi adımı tamamlanmalı."


def _source_line(source: ReviewSourceInvoiceEvidence, line_number: str):
    lines = [item for item in source.invoice.lines if (item.line_number or "").strip() == line_number]
    if not lines:
        raise ProductRemediationEligibilityError(f"Fatura satırı bulunamadı: {line_number}.")
    if len(lines) > 1:
        # A line number is the operator-visible identity; if the source invoice repeats it,
        # "which line" cannot be proven, so nothing is mapped.
        raise ProductRemediationEligibilityError(
            f"Faturada {line_number} numaralı birden fazla satır var; satır kesin belirlenemediği için eşleştirme "
            "yapılmadı. Teknik destek alın."
        )
    return lines[0]


def _product_not_found_lines(reasons: tuple[ManualReviewReason, ...]) -> tuple[str, ...]:
    lines = [
        (reason.line_number or "").strip()
        for reason in reasons
        if reason.code is ManualReviewReasonCode.PRODUCT_NOT_FOUND and (reason.line_number or "").strip()
    ]
    return tuple(dict.fromkeys(lines))


def _ids(rows) -> str:
    return ", ".join(f"#{row.id}" for row in rows)


def _note(
    command: MapExistingProductCommand,
    line_number: str,
    code: str,
    supplier: EffectiveSupplier,
    product: ResolutionProductRecord,
    supplierinfo_id: int | None,
) -> str:
    return (
        f"Product mapping by {command.approved_by}: line {line_number}, supplier partner {supplier.partner_id} "
        f"({supplier.origin.value}), "
        f"seller code {code!r} -> product.product {product.id} (template {product.product_tmpl_id}), "
        f"supplierinfo {supplierinfo_id}."
    )[:1024]


__all__ = ["MapExistingProductUseCase", "STALE_MESSAGE"]
