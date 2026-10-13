"""``CreateLineProductUseCase`` -- see ``app.application.workbench.product_line_creation``.

Order (every check before any write; the only Odoo write is ONE product.template create
carrying its supplier mapping inline):

1. resume an existing creation record of this exact line/version (never a second create);
2. review pending at exactly ``expected_version``; the line carries PRODUCT_NOT_FOUND;
3. seller product code from the immutable source line (none -> refused, never invented);
4. supplier = the review's effective supplier (the partner reclassification matches under);
5. master-data policy: SKU / Internal Reference rules, UoM allowlist, category (RESALE
   rules), manual-only categories, catalog duplicates (read-only);
6. no supplier mapping for (supplier, code) and no identity claim by anyone else;
7. ONE commit: identity claim + consumed CREATE_NEW_PRODUCT authorization +
   ``CREATE_ATTEMPTED`` record -- then the Odoo create;
8. ``PRODUCT_CREATED`` recorded as soon as Odoo returns the ids;
9. post-create verification + supplier mapping read-back, then ``COMPLETED`` committed
   together with the MASTER_DATA_CHANGED reclassification.

A failure that proves no Odoo write happened (gate, validation, auth refusal) deletes the
attempt and releases the claim. Any other failure after step 7 is "outcome unknown": the
record stays ``CREATE_ATTEMPTED`` and the next submission recovers it deterministically
through the atomic supplier mapping (see ``_recover``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.application.commands.product_remediation import CreateProductCommand, ValidatedSupplierLink
from app.application.effective_supplier import EffectiveSupplier, EffectiveSupplierResolverPort
from app.application.exceptions.product_remediation import (
    ProductWriteAuthenticationError,
    ProductWriteAuthorizationError,
    ProductWriteSafetyGateError,
    ProductWriteValidationError,
)
from app.application.ports.existing_supplier_info_reader import ExistingSupplierInfoReader
from app.application.ports.product_writer import ProductWriter
from app.application.services import UnitOfWork
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    ProductRemediationContractError,
    ProductRemediationEligibilityError,
    ProductRemediationRaceError,
    ProductRemediationSupplierUnresolvedError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
)
from app.application.workbench.ports import ReviewQueueReader, ReviewSourceInvoiceEvidenceReader
from app.application.workbench.product_line_creation import (
    CreatedProductReader,
    CreatedProductSnapshot,
    CreateLineProductCommand,
    CreateLineProductResult,
    ProductIdentityClaimStore,
    ProductLineCreateDuplicateError,
    ProductLineCreateInFlightError,
    ProductLineCreateOutcomeUnknownError,
    ProductLineCreateReconciliationRequiredError,
    ProductLineCreation,
    ProductLineCreationRepository,
    ProductLineCreationStatus,
)
from app.application.workbench.product_line_creation_policy import ProductLineCreationPolicy, ValidatedNewProduct
from app.application.workbench.product_mapping import ProductMappingSellerCodeMissingError
from app.application.workbench.product_mapping_use_cases import Reclassifier
from app.application.workbench.product_remediation import (
    CLAIM_OWNER_PRODUCT_LINE_CREATION,
    ExistingSupplierInfo,
    normalize_seller_item_code,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import (
    ReclassifyReviewCommand,
    ReviewReclassificationResult,
    ReviewReclassificationTrigger,
)
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationRepository,
    product_line_creation_authorization_consumer_id,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode
from app.domain.invoice import InternalInvoice

STALE_MESSAGE = "İnceleme bu istekten sonra değişti; güncel kaydı kontrol edip tekrar gönderin."
#: An Odoo request outlives its HTTP client: a create whose response was lost may still
#: commit server-side. Its absence proves nothing until this grace has passed. Odoo Online's
#: server-side request time limit is not documented for this instance, so the grace is far
#: above any plausible worker limit; the cost is only a rare wait for the operator.
DEFAULT_CREATE_OUTCOME_GRACE = timedelta(minutes=30)
#: Column bound of the stored supplier product name (the invoice line description).
MAX_SUPPLIER_PRODUCT_NAME_LENGTH = 512

# Raised by OdooProductWriter BEFORE the network call (gate, payload validation) or when Odoo
# refused the request outright: certain that no product.template was created.
_CERTAIN_NO_WRITE_EXCEPTIONS = (
    ProductWriteSafetyGateError,
    ProductWriteValidationError,
    ProductWriteAuthenticationError,
    ProductWriteAuthorizationError,
)


class _CategoryVerifier(Protocol):
    def verify_created_product(
        self,
        *,
        company_id: int,
        product_id: int,
        product_template_id: int,
        categ_id: int | None,
        is_storable: bool,
        resale: bool,
    ) -> None: ...

    def purpose_is_resale(self, *, review_id: str, company_id: int, review_version: int) -> bool: ...


class CreateLineProductUseCase:
    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        effective_supplier_resolver: EffectiveSupplierResolverPort,
        policy: ProductLineCreationPolicy,
        category_verifier: _CategoryVerifier,
        creation_repository: ProductLineCreationRepository,
        identity_claims: ProductIdentityClaimStore,
        existing_supplier_info_reader: ExistingSupplierInfoReader,
        product_writer: ProductWriter,
        created_product_reader: CreatedProductReader,
        reclassifier: Reclassifier,
        unit_of_work: UnitOfWork,
        write_authorization_repository: WriteAuthorizationRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        outcome_grace: timedelta = DEFAULT_CREATE_OUTCOME_GRACE,
    ) -> None:
        self._review_reader = review_reader
        self._source_invoice_reader = source_invoice_reader
        self._effective_supplier_resolver = effective_supplier_resolver
        self._policy = policy
        self._category_verifier = category_verifier
        self._creations = creation_repository
        self._claims = identity_claims
        self._existing_supplier_info_reader = existing_supplier_info_reader
        self._product_writer = product_writer
        self._created_product_reader = created_product_reader
        self._reclassifier = reclassifier
        self._unit_of_work = unit_of_work
        self._write_authorization_repository = write_authorization_repository
        self._clock = clock
        self._outcome_grace = outcome_grace

    async def execute(self, command: CreateLineProductCommand) -> CreateLineProductResult:
        if not isinstance(command, CreateLineProductCommand):
            raise ProductRemediationContractError("A canonical CreateLineProductCommand is required.")
        line_number = command.line_number.strip()
        existing = self._creations.find(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            line_number=line_number,
        )
        if existing is not None:
            return await self._resume(command, existing)
        return await self._start(command, line_number)

    # ------------------------------------------------------------------ fresh attempt

    async def _start(self, command: CreateLineProductCommand, line_number: str) -> CreateLineProductResult:
        self._require_eligible_review(command, line_number)
        source = self._source_invoice_reader.get(review_id=command.review_id, company_id=command.company_id)
        line = _source_line(source, line_number)
        seller_item_code = normalize_seller_item_code(line.seller_item_code)
        if seller_item_code is None:
            raise ProductMappingSellerCodeMissingError(
                f"Satır {line_number} için faturada satıcı ürün kodu yok; tedarikçiye özel ürün kaydı "
                "oluşturulamaz. Teknik destek alın."
            )
        supplier = self._effective_supplier(command, source.invoice)
        validated = self._policy.validate(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            seller_item_code=seller_item_code,
            intent=command.intent,
            source_unit_code=line.unit_code,
            invoice_seller_codes=_invoice_seller_codes(source),
            supplier_partner_id=supplier.partner_id,
            source_supplier_tax_number=source.invoice.supplier.tax_number,
            source_description=line.description,
        )
        await self._require_no_supplier_mapping(command, supplier.partner_id, seller_item_code)
        self._require_identity_unclaimed(command, supplier.partner_id, seller_item_code, line_number)

        attempt = ProductLineCreation(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            line_number=line_number,
            status=ProductLineCreationStatus.CREATE_ATTEMPTED,
            supplier_partner_id=supplier.partner_id,
            seller_item_code=seller_item_code,
            intent=command.intent,
            approved_by=command.approved_by,
            create_attempted_at=self._clock(),
            supplier_product_name=(line.description or "").strip()[:MAX_SUPPLIER_PRODUCT_NAME_LENGTH] or None,
        )
        try:
            # One commit BEFORE the Odoo call: the claim is the cross-review single-winner
            # barrier, the record is the crash-recovery state, the authorization is consumed.
            self._claims.claim(attempt.identity_claim())
            authorization = self._claim_authorization(command, line_number)
            attempt = self._creations.insert_attempt(attempt)
            self._unit_of_work.commit()
        except ProductRemediationRaceError as exc:
            self._unit_of_work.rollback()
            raise ProductLineCreateInFlightError(
                f"'{seller_item_code}' kodu için başka bir yeni ürün isteği şu anda işleniyor; birkaç dakika sonra "
                "güncel kaydı kontrol edin."
            ) from exc
        except BaseException:
            self._unit_of_work.rollback()
            raise
        return await self._create(command, attempt, validated, authorization)

    async def _create(
        self,
        command: CreateLineProductCommand,
        attempt: ProductLineCreation,
        validated: ValidatedNewProduct,
        authorization: WriteAuthorizationRecord | None,
    ) -> CreateLineProductResult:
        intent = attempt.intent
        try:
            written = await self._product_writer.create_product(
                CreateProductCommand(
                    name=intent.product_name,
                    type=intent.product_type,
                    uom_id=intent.uom_id,
                    is_storable=intent.is_storable,
                    default_code=intent.internal_reference,
                    approved_by=attempt.approved_by,
                    authorization=authorization,
                    category=validated.category,
                    supplier_link=ValidatedSupplierLink(
                        partner_id=attempt.supplier_partner_id,
                        product_code=attempt.seller_item_code,
                        company_id=attempt.company_id,
                        product_name=attempt.supplier_product_name,
                    ),
                )
            )
        except _CERTAIN_NO_WRITE_EXCEPTIONS:
            self._unit_of_work.rollback()
            self._discard(attempt)
            raise
        except BaseException as exc:
            self._unit_of_work.rollback()
            raise ProductLineCreateOutcomeUnknownError(
                "Odoo'ya ürün oluşturma isteği gönderildi ancak sonucu doğrulanamadı; ürün oluşmuş olabilir. "
                f"{_minutes(self._outcome_grace)} dakika sonra aynı isteği tekrar gönderin: Hub önce Odoo'yu "
                "kontrol eder, ikinci bir ürün oluşturmaz."
            ) from exc

        created = self._creations.advance(
            attempt,
            expected_status=ProductLineCreationStatus.CREATE_ATTEMPTED,
            new_status=ProductLineCreationStatus.PRODUCT_CREATED,
            product_template_id=written.template_id,
            product_id=written.product_id,
        )
        self._unit_of_work.commit()
        return await self._complete(command, created, created_product=True)

    # ------------------------------------------------------------------ completion

    async def _complete(
        self, command: CreateLineProductCommand, created: ProductLineCreation, *, created_product: bool
    ) -> CreateLineProductResult:
        assert created.product_template_id is not None and created.product_id is not None
        self._category_verifier.verify_created_product(
            company_id=created.company_id,
            product_id=created.product_id,
            product_template_id=created.product_template_id,
            categ_id=created.intent.categ_id,
            is_storable=created.intent.is_storable,
            resale=self._category_verifier.purpose_is_resale(
                review_id=created.review_id, company_id=created.company_id, review_version=created.review_version
            ),
        )
        supplierinfo = await self._linked_supplier_mapping(created)
        try:
            reclass = await self._reclassifier.execute(
                ReclassifyReviewCommand(
                    review_id=created.review_id,
                    company_id=created.company_id,
                    expected_version=created.review_version,
                    trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
                    note=_note(created, supplierinfo.id),
                )
            )
            completed = self._creations.advance(
                created,
                expected_status=ProductLineCreationStatus.PRODUCT_CREATED,
                new_status=ProductLineCreationStatus.COMPLETED,
                supplierinfo_id=supplierinfo.id,
            )
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise
        return _result(completed, reclass, created_product=created_product, already_applied=False)

    async def _linked_supplier_mapping(self, created: ProductLineCreation) -> ExistingSupplierInfo:
        rows = await self._existing_supplier_info_reader.find_existing(
            partner_id=created.supplier_partner_id, product_code=created.seller_item_code, company_id=created.company_id
        )
        if (
            len(rows) != 1
            or rows[0].product_tmpl_id != created.product_template_id
            or rows[0].product_id not in (None, created.product_id)
        ):
            self._mark_reconciliation(created)
            raise ProductLineCreateReconciliationRequiredError(
                f"Oluşturulan ürünün (#{created.product_id}) tedarikçi kaydı Odoo'da beklenen biçimde bulunamadı; "
                "satır eşleştirilmedi. Teknik destek alın."
            )
        return rows[0]

    # ------------------------------------------------------------------ resume / recovery

    async def _resume(
        self, command: CreateLineProductCommand, existing: ProductLineCreation
    ) -> CreateLineProductResult:
        same_intent = existing.intent == command.intent
        status = existing.status
        if status is ProductLineCreationStatus.NEEDS_RECONCILIATION:
            raise ProductLineCreateReconciliationRequiredError(
                "Bu satırın önceki ürün oluşturma denemesi Odoo ile uyuşmuyor ve teknik inceleme bekliyor; "
                "yeni ürün oluşturulmadı. Teknik destek alın."
            )
        if status is ProductLineCreationStatus.COMPLETED:
            if not same_intent:
                raise ProductLineCreateDuplicateError(
                    f"Bu satır için zaten yeni ürün oluşturuldu (#{existing.product_id}); ikinci bir ürün "
                    "oluşturulmadı."
                )
            return self._already_applied(existing)
        if not same_intent:
            if status is ProductLineCreationStatus.PRODUCT_CREATED:
                raise ProductLineCreateDuplicateError(
                    f"Bu satır için zaten yeni ürün oluşturuldu (#{existing.product_id}); farklı bilgilerle ikinci "
                    "bir ürün oluşturulmadı."
                )
            raise ProductLineCreateInFlightError(
                "Bu satır için farklı bilgilerle gönderilmiş bir ürün oluşturma isteğinin sonucu henüz "
                "doğrulanmadı; önce o isteği aynı bilgilerle tekrar gönderin."
            )
        if status is ProductLineCreationStatus.PRODUCT_CREATED:
            return await self._complete(command, existing, created_product=False)
        return await self._recover(command, existing)

    async def _recover(
        self, command: CreateLineProductCommand, attempt: ProductLineCreation
    ) -> CreateLineProductResult:
        """``CREATE_ATTEMPTED`` with no recorded outcome: decide from the atomic supplier mapping.

        * exactly one mapping whose template matches the recorded intent -> adopt it;
        * a mismatching or ambiguous mapping -> NEEDS_RECONCILIATION (never guessed);
        * no mapping inside the grace -> the create may still be in flight: refuse for now;
        * no mapping after the grace -> the create provably never committed: discard the
          attempt (and its claim) and run the whole fresh path again.
        """

        rows = await self._existing_supplier_info_reader.find_existing(
            partner_id=attempt.supplier_partner_id, product_code=attempt.seller_item_code, company_id=attempt.company_id
        )
        if len(rows) == 1:
            snapshot = (
                self._created_product_reader.read_template(rows[0].product_tmpl_id)
                if rows[0].product_tmpl_id is not None
                else None
            )
            variant_id = _adoptable_variant(attempt, rows[0], snapshot)
            if variant_id is not None and snapshot is not None:
                created = self._creations.advance(
                    attempt,
                    expected_status=ProductLineCreationStatus.CREATE_ATTEMPTED,
                    new_status=ProductLineCreationStatus.PRODUCT_CREATED,
                    product_template_id=snapshot.template_id,
                    product_id=variant_id,
                )
                self._unit_of_work.commit()
                return await self._complete(command, created, created_product=True)
        if rows:
            self._mark_reconciliation(attempt)
            raise ProductLineCreateReconciliationRequiredError(
                f"'{attempt.seller_item_code}' kodu için Odoo'daki tedarikçi kaydı bu isteğin ürün bilgileriyle "
                "uyuşmuyor; ikinci bir ürün oluşturulmadı. Teknik destek alın."
            )
        retry_at = attempt.create_attempted_at + self._outcome_grace
        if self._clock() < retry_at:
            raise ProductLineCreateInFlightError(
                "Önceki ürün oluşturma denemesinin sonucu henüz kesinleşmedi; "
                f"{retry_at.astimezone(UTC):%H:%M} UTC sonrasında aynı isteği tekrar gönderin."
            )
        self._discard(attempt)
        return await self._start(command, attempt.line_number)

    # ------------------------------------------------------------------ checks

    def _require_eligible_review(self, command: CreateLineProductCommand, line_number: str) -> None:
        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )
        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewStateConflictError(STALE_MESSAGE)
        if review.version != command.expected_version:
            raise ReviewVersionConflictError(STALE_MESSAGE)
        if line_number not in _product_not_found_lines(review.review_reasons):
            raise ProductRemediationEligibilityError(
                f"Satır {line_number} için yeni ürün gerekmiyor (bu satırda 'Ürün Odoo'da bulunamadı' engeli yok)."
            )

    def _effective_supplier(self, command: CreateLineProductCommand, invoice: InternalInvoice) -> EffectiveSupplier:
        resolution = self._effective_supplier_resolver.resolve(
            review_id=command.review_id, company_id=command.company_id, invoice=invoice
        )
        if resolution.supplier is None:
            raise ProductRemediationSupplierUnresolvedError(
                resolution.failure
                or "Bu incelemede tedarikçi henüz kesin olarak eşleşmedi; önce tedarikçi adımı tamamlanmalı."
            )
        return resolution.supplier

    async def _require_no_supplier_mapping(self, command: CreateLineProductCommand, partner_id: int, code: str) -> None:
        existing = await self._existing_supplier_info_reader.find_existing(
            partner_id=partner_id, product_code=code, company_id=command.company_id
        )
        if existing:
            raise ProductLineCreateDuplicateError(
                f"Bu tedarikçinin '{code}' ürün kodu Odoo'da zaten bir ürüne bağlı (tedarikçi ürün kaydı "
                f"{', '.join(f'#{row.id}' for row in existing)}); yeni ürün oluşturulmadı."
            )

    def _require_identity_unclaimed(
        self, command: CreateLineProductCommand, partner_id: int, code: str, line_number: str
    ) -> None:
        """No other owner may hold the supplier-product identity claim.

        The one exception is THIS line's own unresolved attempt from an older review
        version (the review moved after a lost response, so that attempt can no longer be
        resumed at its own version). Its supplier mapping is already proven absent by the
        caller; after the in-flight grace the old attempt is discarded and its claim
        released, inside the grace the request waits. Anything else fails closed.
        """

        claim = self._claims.find(
            company_id=command.company_id, resolved_supplier_partner_id=partner_id, seller_item_code=code
        )
        if claim is None:
            return
        own_line = claim.owner_kind == CLAIM_OWNER_PRODUCT_LINE_CREATION and (
            claim.owner_review_id,
            claim.owner_company_id,
            claim.owner_line_number,
        ) == (command.review_id, command.company_id, line_number)
        previous = (
            self._creations.find(
                review_id=claim.owner_review_id,
                company_id=claim.owner_company_id,
                review_version=claim.owner_review_version,
                line_number=claim.owner_line_number,
            )
            if own_line
            else None
        )
        if previous is None:
            raise ProductLineCreateDuplicateError(
                f"'{code}' kodu için başka bir inceleme satırında ({claim.owner_review_id}, satır "
                f"{claim.owner_line_number}) yeni ürün oluşturma kaydı var; ikinci bir ürün oluşturulmadı."
            )
        if previous.status is ProductLineCreationStatus.NEEDS_RECONCILIATION:
            raise ProductLineCreateReconciliationRequiredError(
                "Bu satırın önceki bir sürümdeki ürün oluşturma denemesi teknik inceleme bekliyor; yeni ürün "
                "oluşturulmadı. Teknik destek alın."
            )
        if previous.status is not ProductLineCreationStatus.CREATE_ATTEMPTED:
            raise ProductLineCreateDuplicateError(
                f"Bu satır için önceki bir sürümde zaten yeni ürün oluşturuldu (#{previous.product_id}); ikinci "
                "bir ürün oluşturulmadı."
            )
        retry_at = previous.create_attempted_at + self._outcome_grace
        if self._clock() < retry_at:
            raise ProductLineCreateInFlightError(
                "Bu satırın önceki ürün oluşturma denemesinin sonucu henüz kesinleşmedi; "
                f"{retry_at.astimezone(UTC):%H:%M} UTC sonrasında tekrar gönderin."
            )
        self._discard(previous)

    def _claim_authorization(
        self, command: CreateLineProductCommand, line_number: str
    ) -> WriteAuthorizationRecord | None:
        if command.authorization_id is None:
            return None
        if self._write_authorization_repository is None:
            raise ProductRemediationContractError("Runtime authorization is not supported by this workflow.")
        return self._write_authorization_repository.claim_and_consume(
            company_id=command.company_id,
            review_id=command.review_id,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=command.expected_version,
            authorization_id=command.authorization_id,
            execution_id=product_line_creation_authorization_consumer_id(
                company_id=command.company_id,
                review_id=command.review_id,
                expected_version=command.expected_version,
                line_number=line_number,
                intent_digest=command.intent.digest(),
            ),
        )

    # ------------------------------------------------------------------ state helpers

    def _discard(self, attempt: ProductLineCreation) -> None:
        """Only for a proven no-write: drop the attempt and release this line's claim."""

        try:
            self._creations.discard_attempt(attempt)
            self._claims.release(attempt.identity_claim())
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise

    def _mark_reconciliation(self, creation: ProductLineCreation) -> None:
        try:
            self._creations.advance(
                creation,
                expected_status=creation.status,
                new_status=ProductLineCreationStatus.NEEDS_RECONCILIATION,
            )
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise

    def _already_applied(self, completed: ProductLineCreation) -> CreateLineProductResult:
        assert completed.product_template_id is not None and completed.product_id is not None
        assert completed.supplierinfo_id is not None
        return CreateLineProductResult(
            review_id=completed.review_id,
            company_id=completed.company_id,
            line_number=completed.line_number,
            seller_item_code=completed.seller_item_code,
            supplier_partner_id=completed.supplier_partner_id,
            product_name=completed.intent.product_name,
            product_template_id=completed.product_template_id,
            product_id=completed.product_id,
            supplierinfo_id=completed.supplierinfo_id,
            created_product=False,
            already_applied=True,
            previous_version=completed.review_version,
            current_version=completed.review_version + 1,
            line_resolved=True,
        )


def _adoptable_variant(
    attempt: ProductLineCreation, row: ExistingSupplierInfo, snapshot: CreatedProductSnapshot | None
) -> int | None:
    """The single variant of a template that matches the recorded intent exactly, else None."""

    intent = attempt.intent
    if snapshot is None or row.product_tmpl_id != snapshot.template_id or len(snapshot.variant_ids) != 1:
        return None
    if (
        (snapshot.name or "").strip() != intent.product_name
        or snapshot.default_code != intent.internal_reference
        or snapshot.product_type != intent.product_type
        or snapshot.uom_id != intent.uom_id
        or snapshot.categ_id != intent.categ_id
        or (snapshot.is_storable is not None and snapshot.is_storable is not intent.is_storable)
    ):
        return None
    variant_id = snapshot.variant_ids[0]
    return variant_id if row.product_id in (None, variant_id) else None


def _result(
    completed: ProductLineCreation,
    reclass: ReviewReclassificationResult,
    *,
    created_product: bool,
    already_applied: bool,
) -> CreateLineProductResult:
    assert completed.product_template_id is not None and completed.product_id is not None
    assert completed.supplierinfo_id is not None
    remaining = _product_not_found_lines(reclass.new_review_reasons)
    return CreateLineProductResult(
        review_id=completed.review_id,
        company_id=completed.company_id,
        line_number=completed.line_number,
        seller_item_code=completed.seller_item_code,
        supplier_partner_id=completed.supplier_partner_id,
        product_name=completed.intent.product_name,
        product_template_id=completed.product_template_id,
        product_id=completed.product_id,
        supplierinfo_id=completed.supplierinfo_id,
        created_product=created_product,
        already_applied=already_applied,
        previous_version=reclass.from_version,
        current_version=reclass.to_version,
        line_resolved=completed.line_number not in remaining,
        remaining_product_lines=remaining,
    )


def _source_line(source: ReviewSourceInvoiceEvidence, line_number: str):
    lines = [item for item in source.invoice.lines if (item.line_number or "").strip() == line_number]
    if not lines:
        raise ProductRemediationEligibilityError(f"Fatura satırı bulunamadı: {line_number}.")
    if len(lines) > 1:
        raise ProductRemediationEligibilityError(
            f"Faturada {line_number} numaralı birden fazla satır var; satır kesin belirlenemediği için ürün "
            "oluşturulmadı. Teknik destek alın."
        )
    return lines[0]


def _invoice_seller_codes(source: ReviewSourceInvoiceEvidence) -> tuple[str, ...]:
    codes = (normalize_seller_item_code(line.seller_item_code) for line in source.invoice.lines)
    return tuple(dict.fromkeys(code for code in codes if code is not None))


def _product_not_found_lines(reasons: tuple[ManualReviewReason, ...]) -> tuple[str, ...]:
    lines = [
        (reason.line_number or "").strip()
        for reason in reasons
        if reason.code is ManualReviewReasonCode.PRODUCT_NOT_FOUND and (reason.line_number or "").strip()
    ]
    return tuple(dict.fromkeys(lines))


def _minutes(value: timedelta) -> int:
    return max(1, int(value.total_seconds() // 60))


def _note(created: ProductLineCreation, supplierinfo_id: int) -> str:
    intent = created.intent
    return (
        f"New product by {created.approved_by}: line {created.line_number}, supplier partner "
        f"{created.supplier_partner_id}, seller code {created.seller_item_code!r} -> product.product "
        f"{created.product_id} (template {created.product_template_id}, {intent.kind.value}, categ "
        f"{intent.categ_id}, uom {intent.uom_id}), supplierinfo {supplierinfo_id}."
    )[:1024]


__all__ = ["DEFAULT_CREATE_OUTCOME_GRACE", "STALE_MESSAGE", "CreateLineProductUseCase"]
