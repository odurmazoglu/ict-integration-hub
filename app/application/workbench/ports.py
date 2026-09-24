from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.application.workbench.accounting_resolution import ReviewAccountingResolution
from app.application.workbench.billing_authoring import (
    ValidatedWorkbenchBillingAuthoring,
    WorkbenchBillingAuthoringRow,
)
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import ReviewDecisionAcknowledgement, ReviewItem, ReviewQueueResult
from app.application.workbench.evidence import (
    ReviewClassificationEvidence,
    ReviewExecutionBillingEvidence,
    ReviewExecutionEvidence,
    ReviewSourceInvoiceEvidence,
)
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.one_off_vendor_retirement import OneOffVendorRetirement, OneOffVendorRetirementStatus
from app.application.workbench.product_remediation import (
    ProductIdentityClaim,
    ProductRemediationReservation,
    ProductReservationStatus,
)
from app.application.workbench.projection import (
    OdooWorkbenchDecisionCandidate,
    ProjectionPublishResult,
    WorkbenchProjection,
)
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountRecord,
    ProductPurchaseAccountRecord,
    PurchaseAccountRecord,
)
from app.application.workbench.purchase_purpose import PurchasePurposeResolution
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery
from app.application.workbench.reclassification import (
    ReviewReclassificationProposal,
    ReviewReclassificationResult,
)
from app.application.workbench.selected_expense_account_resolution import ResolutionAccountRecord
from app.application.workbench.selected_product_resolution import ResolutionProductRecord
from app.application.workbench.supplier_remediation import SupplierRemediationEffect
from app.application.workbench.supplier_resolution import ResolutionPartnerRecord, SupplierResolution
from app.billing.dto import CustomerInvoiceBillingInstruction

if TYPE_CHECKING:
    from app.application.execution.contracts import ExecutionSourceInvoice


class ReviewQueueReader(Protocol):
    """Read-only port for future Workbench review queue adapters."""

    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        pass

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        pass


class ReviewItemWriter(Protocol):
    """Write port for idempotent creation of pending Workbench review items."""

    def create_review_item(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        source_invoice_evidence: ReviewSourceInvoiceEvidence | None = None,
    ) -> ReviewItem:
        pass

    def create_review_item_with_execution_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
        classification_evidence: ReviewClassificationEvidence | None = None,
        source_invoice_evidence: ReviewSourceInvoiceEvidence | None = None,
    ) -> ReviewItem:
        pass

    def create_review_item_with_classification_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        classification_evidence: ReviewClassificationEvidence,
        source_invoice_evidence: ReviewSourceInvoiceEvidence | None = None,
    ) -> ReviewItem:
        pass

    def create_review_item_with_billing_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
        classification_evidence: ReviewClassificationEvidence | None = None,
    ) -> ReviewItem:
        pass

    def create_review_item_with_execution_and_billing_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
        classification_evidence: ReviewClassificationEvidence | None = None,
    ) -> ReviewItem:
        pass


class ReviewSourceInvoiceEvidenceReader(Protocol):
    """Read-only port for the immutable canonical source invoice pinned to a review."""

    def get(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> ReviewSourceInvoiceEvidence:
        pass


class ReviewReclassificationWriter(Protocol):
    """Write port for one atomic non-destructive review reclassification transition."""

    def reclassify_review(self, proposal: ReviewReclassificationProposal) -> ReviewReclassificationResult:
        pass


class SupplierResolutionPartnerReader(Protocol):
    """Read-only port for reading one Odoo ``res.partner`` when validating MATCH_EXISTING."""

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        pass


class SelectedProductReader(Protocol):
    """Read-only port for reading Odoo ``product.product`` records by id.

    Used only to validate an explicit ``LineResolution.selected_product_id`` before
    it is pinned as accepted execution evidence -- never during Vendor Bill execution.
    """

    def find_products_by_ids(self, product_ids: tuple[int, ...]) -> tuple[ResolutionProductRecord, ...]:
        pass


class SelectedAccountReader(Protocol):
    """Read-only port for reading Odoo ``account.account`` records by id.

    Used only to validate an explicit ``LineResolution.expense_account_id`` (P0-PROD-08G)
    before it is accepted as part of a review decision -- never during Vendor Bill
    execution, and never used to write.
    """

    def find_accounts_by_ids(self, account_ids: tuple[int, ...]) -> tuple[ResolutionAccountRecord, ...]:
        pass


class ExpenseAccountCandidateReader(Protocol):
    """Read-only port for the server-controlled ``account.account`` lookup (P0-PROD-15P).

    There is no method here that accepts a caller-supplied model, domain, or field list --
    the adapter behind this port owns those entirely; the caller may only narrow by an
    optional free-text ``query`` and is always scoped by ``company_id``.
    """

    def find_candidates(self, *, company_id: int, query: str | None) -> tuple[ExpenseAccountCandidate, ...]:
        pass

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        """The one candidate matching ``account_id``, or ``None`` if it does not exist, is not
        an eligible operating-expense account, or is not scoped to ``company_id``."""


class PurchaseAccountDiscoveryReader(Protocol):
    """Read-only port for product/category purchase-account configuration (P0-PROD-18D).

    Like :class:`ExpenseAccountCandidateReader`, no method accepts a caller-supplied
    model, domain, or field list -- the adapter owns all of them.
    """

    def accessible_company_ids(self) -> tuple[int, ...]:
        """Every Odoo company visible to the integration user, ascending."""

    def list_categories(self) -> tuple[CategoryPurchaseAccountRecord, ...]:
        """Every product category, bounded; raises rather than truncating."""

    def find_category(self, *, category_id: int) -> CategoryPurchaseAccountRecord | None:
        pass

    def find_product(self, *, company_id: int, product_id: int) -> ProductPurchaseAccountRecord | None:
        """The product (active or archived) if shared or owned by ``company_id``, else ``None``."""

    def find_accounts(self, *, company_id: int, account_ids: tuple[int, ...]) -> tuple[PurchaseAccountRecord, ...]:
        """The subset of ``account_ids`` readable for ``company_id``."""


class SupplierResolutionWriter(Protocol):
    """Append-only port for persisting an explicit supplier-resolution decision."""

    def create_supplier_resolution(self, resolution: SupplierResolution) -> SupplierResolution:
        pass

    def reserve_supplier_resolution(self, resolution: SupplierResolution) -> SupplierResolution:
        """Single-winner reservation: the concurrent INSERT-race loser is raised out, never returned."""

    def get_supplier_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> SupplierResolution:
        pass


class SupplierRemediationEffectWriter(Protocol):
    """Append-only port for the completed effect of a supplier remediation."""

    def create_remediation_effect(self, effect: SupplierRemediationEffect) -> SupplierRemediationEffect:
        pass

    def find_remediation_effect(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> SupplierRemediationEffect | None:
        pass

    def find_latest_remediation_effect(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> SupplierRemediationEffect | None:
        """The most recently recorded effect for this review, regardless of exact version."""

    def find_one_off_vendor_effect_by_partner_id(
        self,
        *,
        company_id: int,
        resolved_partner_id: int,
    ) -> SupplierRemediationEffect | None:
        """The (at most one, by construction) ONE_OFF_VENDOR effect that created/reused this
        exact Odoo partner, if any (P0-PROD-08H).

        This is the Hub's sole source of truth for "did we create/own this partner via
        ONE_OFF_VENDOR" -- an exact-VAT match against a partner with no such effect is a
        pre-existing partner (e.g. a permanent supplier) the Hub must never silently
        adopt as retirement-eligible. Read-only; never used to decide anything about
        MATCH_EXISTING or CREATE_PERMANENT_SUPPLIER partners.
        """


class PurchasePurposeResolutionWriter(Protocol):
    """Append-only port for a review-scoped purchase-purpose resolution (P0-PROD-15T)."""

    def create_purchase_purpose_resolution(self, resolution: PurchasePurposeResolution) -> PurchasePurposeResolution:
        pass

    def find_purchase_purpose_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> PurchasePurposeResolution | None:
        pass


class ReviewAccountingResolutionReader(Protocol):
    """Read-only port consulted by ``ReclassifyWorkbenchReviewUseCase`` (P0-PROD-15T)."""

    def find_latest_accounting_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> ReviewAccountingResolution | None:
        """The most recently recorded resolution for this review, regardless of exact version."""


class ReviewAccountingResolutionWriter(ReviewAccountingResolutionReader, Protocol):
    """Append-only port for a review-scoped accounting resolution (P0-PROD-15T)."""

    def create_accounting_resolution(self, resolution: ReviewAccountingResolution) -> ReviewAccountingResolution:
        pass

    def find_accounting_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewAccountingResolution | None:
        pass


class ReviewExecutionEvidenceRecoveryWriter(Protocol):
    """Port for the review-scoped Stage-1 execution-evidence recovery/repair
    operation (P0-PROD-15Z). Distinct from ``ReviewExecutionEvidenceReader``
    (which raises when evidence is missing, for the decision-submission path):
    ``find_execution_evidence`` returns ``None`` instead, so recovery can decide
    between "create" and "already applied" without an exception-driven control
    flow. ``create_execution_evidence_for_current_version`` never advances the
    review version, never writes ``WorkbenchReviewItem``, and never writes a
    ``WorkbenchReviewReclassification`` event -- it inserts exactly one
    ``WorkbenchReviewExecutionEvidence`` row, gated on the review still being
    pending review at ``expected_version`` at the moment of insert.
    """

    def find_execution_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewExecutionEvidence | None:
        pass

    def create_execution_evidence_for_current_version(
        self,
        *,
        review_id: str,
        company_id: int,
        expected_version: int,
        evidence: ReviewExecutionEvidence,
    ) -> ReviewExecutionEvidence:
        pass


class OneOffVendorRetirementWriter(Protocol):
    """Durable state-machine persistence for one review's ONE_OFF_VENDOR archive lifecycle."""

    def create_retirement(self, retirement: OneOffVendorRetirement) -> OneOffVendorRetirement:
        pass

    def find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> OneOffVendorRetirement | None:
        pass

    def find_latest_for_review(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> OneOffVendorRetirement | None:
        """The most recent retirement row for this review, regardless of version.

        A review normally carries at most one ONE_OFF_VENDOR retirement row -- once
        created, SUPPLIER_NOT_FOUND is cleared and the resolution is never repeated. This
        exists for the post-Vendor-Bill retirement trigger (P0-PROD-08I), which knows only
        ``(review_id, company_id)`` at execution time, never the historical review version
        the resolution was recorded at.
        """

    def advance(
        self,
        retirement: OneOffVendorRetirement,
        *,
        expected_status: OneOffVendorRetirementStatus,
        new_status: OneOffVendorRetirementStatus,
    ) -> OneOffVendorRetirement:
        pass


class VendorBillExecutionEvidenceReader(Protocol):
    """Read-only port for durable Vendor Bill execution evidence (P0-PROD-08H).

    Answers exactly one question: does a terminal, successful VENDOR_BILL
    execution step already exist for this review, with a known produced Odoo
    identity? Backed by the existing ``workflow_executions``/
    ``workflow_execution_steps`` persistence -- no new tracking is introduced for
    this fact, it already exists durably.
    """

    def has_successful_vendor_bill(self, *, review_id: str, company_id: int) -> bool:
        pass


class ReviewDecisionWriter(Protocol):
    """Write port for explicit user decision submission against a pending review item."""

    def has_matching_review_decision(self, command: ReviewDecisionCommand) -> bool:
        pass

    def submit_review_decision(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        pass

    def submit_review_decision_with_execution_evidence(
        self,
        command: ReviewDecisionCommand,
        evidence: ExecutionSourceInvoice,
    ) -> ReviewDecisionAcknowledgement:
        pass

    def submit_review_decision_with_execution_and_billing_evidence(
        self,
        command: ReviewDecisionCommand,
        evidence: ExecutionSourceInvoice,
        billing_instructions: tuple[CustomerInvoiceBillingInstruction, ...],
    ) -> ReviewDecisionAcknowledgement:
        pass


class ReviewExecutionEvidenceReader(Protocol):
    """Read-only port for immutable execution source evidence available at review submission time."""

    def get_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        expected_version: int,
    ) -> ExecutionSourceInvoice:
        pass


class ReviewBillingEvidenceReader(Protocol):
    """Read-only port for immutable customer billing evidence available at review submission time."""

    def get_billing_instructions(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> tuple[CustomerInvoiceBillingInstruction, ...]:
        pass


class ReviewClassificationEvidenceReader(Protocol):
    """Read-only port for immutable classification evidence pinned to a review version."""

    def get_classification_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewClassificationEvidence:
        pass


class ReviewBillingEvidenceWriter(Protocol):
    """Write port for append-only Stage 1 customer billing evidence capture."""

    def capture_review_billing_evidence(
        self,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
    ) -> tuple[ReviewExecutionBillingEvidence, ...]:
        pass


class WorkbenchBillingAuthoringReader(Protocol):
    """Read-only port for Odoo-authored Customer Invoice billing terms."""

    def get_billing_authoring(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> tuple[WorkbenchBillingAuthoringRow, ...]:
        pass


class WorkbenchBillingReferenceValidator(Protocol):
    """Read-only exact ERP reference validator for billing authoring rows."""

    def validate_billing_authoring(
        self,
        rows: tuple[WorkbenchBillingAuthoringRow, ...],
        *,
        requested_company_id: int,
    ) -> ValidatedWorkbenchBillingAuthoring:
        pass


class WorkbenchProjectionPublisher(Protocol):
    """Port for publishing Hub-owned review projections to an ERP UI surface."""

    def publish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        pass

    def republish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        """Update an already-created projection row; never create one (fails closed if missing)."""

    def acknowledge_decision(
        self,
        acknowledgement: ReviewDecisionAcknowledgement,
        *,
        odoo_record_id: int,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        clear_ready: bool = False,
    ) -> ProjectionPublishResult:
        pass


class ProductRemediationReservationWriter(Protocol):
    """Durable state-machine persistence for one review-line CREATE_NEW_PRODUCT reservation.

    ``reserve`` is the single-winner cross-process barrier for identity A
    (``review_id, company_id, review_version, line_number``); the concurrent
    INSERT-race loser must be raised out, never returned (mirrors
    ``SupplierResolutionWriter.reserve_supplier_resolution``). ``advance`` performs
    a compare-and-swap status transition guarded by ``expected_status`` so a stray
    duplicate advancement can never silently overwrite already-persisted Odoo identity.
    """

    def reserve(self, reservation: ProductRemediationReservation) -> ProductRemediationReservation:
        """Single-winner reservation: the concurrent INSERT-race loser is raised out, never returned."""

    def find(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
        line_number: str,
    ) -> ProductRemediationReservation | None:
        pass

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
        pass


class ProductIdentityClaimWriter(Protocol):
    """Durable cross-review lock for one supplier-product identity.

    ``claim`` is the single-winner cross-process barrier for identity B
    (``company_id, resolved_supplier_partner_id, seller_item_code``); the
    concurrent INSERT-race loser is raised out, never returned.
    """

    def claim(self, claim: ProductIdentityClaim) -> ProductIdentityClaim:
        """Single-winner claim: the concurrent INSERT-race loser is raised out, never returned."""

    def find(
        self,
        *,
        company_id: int,
        resolved_supplier_partner_id: int,
        seller_item_code: str,
    ) -> ProductIdentityClaim | None:
        pass


class WorkbenchDecisionCandidateReader(Protocol):
    """Port for reading user-submitted decision candidates from an ERP UI surface."""

    def list_ready_decisions(self, *, company_id: int, limit: int) -> tuple[OdooWorkbenchDecisionCandidate, ...]:
        pass

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate:
        pass
