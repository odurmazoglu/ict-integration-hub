"""Read-only review-detail evidence (P0-PROD-15F), made decision-aware in P0-PROD-19F.

Accepting a decision advances the review version, but Stage-1 matching evidence is
keyed by the version the decision was accepted *against* (``decision_version - 1``).
Reading Stage-1 evidence at the post-decision version therefore finds nothing, and
every line's ``product_match`` used to come back ``null`` after acceptance -- even
for automatically matched lines. This reader keeps the two facts apart instead of
collapsing them:

* ``product_match`` -- the matcher's own pre-decision result (Stage-1), unchanged;
* ``effective_resolution`` -- what the accepted decision pinned for execution
  (Stage-2 ``ExecutionSourceInvoice``, the exact evidence execution/preview/readback
  build from), including whether the product was human-selected or automatic.

Nothing here writes, re-matches, or calls Odoo beyond the existing supplier
candidate lookup; ``review_reasons`` are never rewritten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.application.execution.contracts import AcceptedReviewDecision, ExecutionSourceInvoice
from app.application.execution.exceptions import (
    ExecutionPlanningError,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
)
from app.application.expense_mapping import OperatingExpenseMatchStatus, invoice_is_product_identifier_free
from app.application.expense_mapping.exceptions import OperatingExpenseMappingContractError
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.evidence import ReviewExecutionEvidence
from app.application.workbench.exceptions import ReviewDecisionDataIntegrityError, ReviewNotFoundError
from app.application.workbench.selected_product_resolution import HUMAN_SELECTED_MATCHED_BY
from app.application.workflow import WorkflowType
from app.domain.invoice import InvoiceLine
from app.erp.models import Partner
from app.matching import ProductMatchResult, ProductMatchStatus

logger = logging.getLogger(__name__)

EFFECTIVE_STATE_UNAVAILABLE = "Accepted decision evidence could not be loaded safely."

#: ``operating_expense_match.matched_by`` of an accepted review-scoped accounting
#: resolution; mirrors ``app.application.use_cases.effective_decision.
#: REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY`` (pinned equal by a test; not imported,
#: to keep this read model free of the reclassification use-case graph).
REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY = "review_accounting_resolution"

#: Persisted accepted-decision evidence that exists but cannot be trusted. Read models
#: report it as ``effective_state_error`` instead of failing the whole read. Besides the
#: reader's own integrity error, this covers operating-expense evidence that violates
#: its DTO contract (e.g. MATCHED without an account) or the ``ExecutionSourceInvoice``
#: invariants -- both raised while rebuilding the pinned evidence.
ACCEPTED_EVIDENCE_INTEGRITY_ERRORS = (
    ReviewDecisionDataIntegrityError,
    ExecutionSourceInvoiceIntegrityError,
    OperatingExpenseMappingContractError,
    ExecutionPlanningError,
)


class EffectiveLineResolutionKind(StrEnum):
    """How the accepted decision resolved one invoice line for execution."""

    PRODUCT = "product"
    #: An explicit per-line human ``LineResolution.account_only`` decision.
    ACCOUNT_ONLY = "account_only"
    #: OPS-UI-01A-1: the whole invoice is booked to one account by an accepted
    #: review-scoped accounting resolution (P0-PROD-15T).
    ACCOUNTING_RESOLUTION = "accounting_resolution"
    #: OPS-UI-01A-1: the whole invoice is booked to one account by a supplier-wide
    #: operating-expense mapping.
    OPERATING_EXPENSE_MAPPING = "operating_expense_mapping"
    UNRESOLVED = "unresolved"


class EffectiveProductSource(StrEnum):
    """Who chose the effective product: the deterministic matcher or an operator."""

    AUTOMATIC = "automatic"
    HUMAN_SELECTED = "human_selected"


@dataclass(frozen=True, slots=True)
class EffectiveLineResolution:
    """One line's resolution as pinned by the accepted decision (Stage-2 evidence)."""

    kind: EffectiveLineResolutionKind
    product_id: int | None
    product_source: EffectiveProductSource | None
    matched_by: str | None
    match_status: str | None
    expense_account_id: int | None


@dataclass(frozen=True, slots=True)
class AcceptedDecisionSummary:
    """The accepted decision the review's current version was submitted with."""

    decision_id: str | None
    decision_version: int
    decision_type: ReviewDecisionType
    selected_workflow: WorkflowType | None


@dataclass(frozen=True, slots=True)
class SupplierCandidate:
    partner_id: int
    name: str | None
    vat: str | None
    active: bool
    company_type: str | None
    parent_id: int | None
    commercial_partner_id: int | None
    street: str | None
    street2: str | None
    zip_code: str | None
    city: str | None
    state_id: int | None
    country_id: int | None
    email: str | None
    phone: str | None
    mobile: str | None
    website: str | None
    supplier_rank: int | None
    customer_rank: int | None
    company_id: int | None

    @classmethod
    def from_partner(cls, partner: Partner) -> SupplierCandidate:
        return cls(
            partner_id=partner.id,
            name=partner.name,
            vat=partner.tax_number,
            active=partner.active,
            company_type=partner.company_type,
            parent_id=partner.parent_id,
            commercial_partner_id=partner.commercial_partner_id,
            street=partner.street,
            street2=partner.street2,
            zip_code=partner.zip_code,
            city=partner.city,
            state_id=partner.state_id,
            country_id=partner.country_id,
            email=partner.email,
            phone=partner.phone,
            mobile=partner.mobile,
            website=partner.website,
            supplier_rank=partner.supplier_rank,
            customer_rank=partner.customer_rank,
            company_id=partner.company_id,
        )


@dataclass(frozen=True, slots=True)
class SourceLineEvidence:
    line_number: str | None
    description: str | None
    quantity: Decimal | None
    unit_code: str | None
    unit_price: Decimal | None
    gross_amount: Decimal | None
    discount_amount: Decimal | None
    net_amount: Decimal | None
    taxes: tuple[tuple[str | None, Decimal | None, Decimal | None], ...]
    seller_item_code: str | None
    buyer_item_code: str | None
    product_match: ProductMatchResult | None
    effective_resolution: EffectiveLineResolution | None = None

    @classmethod
    def from_line(
        cls,
        line: InvoiceLine,
        product_match: ProductMatchResult | None,
        effective_resolution: EffectiveLineResolution | None = None,
    ) -> SourceLineEvidence:
        discount_amount = (
            sum((discount.amount or Decimal("0")) for discount in line.discounts) if line.discounts else None
        )
        return cls(
            line_number=line.line_number,
            description=line.description,
            quantity=line.quantity,
            unit_code=line.unit_code,
            unit_price=line.unit_price,
            gross_amount=(
                line.line_extension_amount + discount_amount
                if line.line_extension_amount is not None and discount_amount is not None
                else line.line_extension_amount
            ),
            discount_amount=discount_amount,
            net_amount=line.line_extension_amount,
            taxes=tuple((tax.tax_type, tax.rate, tax.tax_amount) for tax in line.taxes),
            seller_item_code=line.seller_item_code,
            buyer_item_code=line.buyer_item_code,
            product_match=product_match,
            effective_resolution=effective_resolution,
        )


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    supplier_candidates: tuple[SupplierCandidate, ...] = ()
    source_lines: tuple[SourceLineEvidence, ...] = ()
    #: The review version whose Stage-1 evidence ``product_match`` was read from.
    product_match_review_version: int | None = None
    accepted_decision: AcceptedDecisionSummary | None = None
    #: Set (and ``effective_resolution`` left ``None``) when persisted accepted-decision
    #: evidence exists but fails integrity checks; the detail stays readable.
    effective_state_error: str | None = None


class ReviewEvidenceReader:
    def __init__(
        self,
        *,
        source_reader,
        execution_reader,
        partner_repository,
        accepted_decision_reader=None,
        accepted_source_reader=None,
    ) -> None:
        self._source_reader = source_reader
        self._execution_reader = execution_reader
        self._partner_repository = partner_repository
        self._accepted_decision_reader = accepted_decision_reader
        self._accepted_source_reader = accepted_source_reader

    def get(self, *, review_id: str, company_id: int, review_version: int) -> ReviewEvidence | None:
        try:
            source = self._source_reader.get(review_id=review_id, company_id=company_id)
        except ReviewNotFoundError:
            return None
        effective_state_error: str | None = None
        try:
            decision = self._accepted_decision(review_id=review_id, company_id=company_id, version=review_version)
            accepted_source = self._accepted_source(decision, review_id=review_id, company_id=company_id)
        except ACCEPTED_EVIDENCE_INTEGRITY_ERRORS:
            logger.warning(
                "workbench.review_detail.effective_state_unavailable",
                extra={"review_id": review_id, "company_id": company_id, "review_version": review_version},
                exc_info=True,
            )
            decision, accepted_source, effective_state_error = None, None, EFFECTIVE_STATE_UNAVAILABLE
        # Stage-1 evidence is keyed by the version a decision is (or was) accepted against.
        stage_one_version = decision.decision_version - 1 if decision is not None else review_version
        execution = self._execution_evidence(
            review_id=review_id, company_id=company_id, review_version=stage_one_version
        )
        product_by_line = (
            {result.line_number: result.result for result in execution.product_match.line_results}
            if execution is not None
            else {}
        )
        effective_by_line = effective_resolutions(accepted_source) if accepted_source is not None else {}
        source_lines = tuple(
            SourceLineEvidence.from_line(
                line, product_by_line.get(line.line_number), effective_by_line.get(line.line_number)
            )
            for line in source.invoice.lines
        )
        tax_number = source.invoice.supplier.tax_number
        candidates = (
            ()
            if not tax_number
            else tuple(
                SupplierCandidate.from_partner(partner)
                for partner in self._partner_repository.find_by_tax_number(tax_number, company_id=company_id)
                if partner.active
            )
        )
        return ReviewEvidence(
            supplier_candidates=candidates,
            source_lines=source_lines,
            product_match_review_version=stage_one_version if execution is not None else None,
            accepted_decision=_decision_summary(decision) if decision is not None else None,
            effective_state_error=effective_state_error,
        )

    def _accepted_decision(self, *, review_id: str, company_id: int, version: int) -> AcceptedReviewDecision | None:
        # Only a decision whose review_version_after is the review's current version
        # governs it: decisions are terminal, so nothing advances the version after one.
        if self._accepted_decision_reader is None or version <= 1:
            return None
        try:
            return self._accepted_decision_reader.get_accepted_decision(
                review_id=review_id, company_id=company_id, decision_version=version
            )
        except ReviewNotFoundError:
            return None

    def _accepted_source(
        self, decision: AcceptedReviewDecision | None, *, review_id: str, company_id: int
    ) -> ExecutionSourceInvoice | None:
        if decision is None or self._accepted_source_reader is None:
            return None
        try:
            return self._accepted_source_reader.get_source_invoice(
                review_id=review_id, company_id=company_id, decision_version=decision.decision_version
            )
        except ExecutionSourceInvoiceNotFoundError:
            # Normal for DISMISS and non-Vendor-Bill decisions: no execution evidence is pinned.
            return None

    def _execution_evidence(
        self, *, review_id: str, company_id: int, review_version: int
    ) -> ReviewExecutionEvidence | None:
        try:
            return self._execution_reader.get_review_execution_evidence(
                review_id=review_id, company_id=company_id, review_version=review_version
            )
        except ReviewNotFoundError:
            return None


def _decision_summary(decision: AcceptedReviewDecision) -> AcceptedDecisionSummary:
    return AcceptedDecisionSummary(
        decision_id=decision.decision_id,
        decision_version=decision.decision_version,
        decision_type=decision.decision_type,
        selected_workflow=decision.selected_workflow,
    )


def effective_resolutions(source: ExecutionSourceInvoice) -> dict[str | None, EffectiveLineResolution]:
    """Per-line resolution exactly as execution consumes it; derived, never re-matched.

    Public since OPS-UI-01A: the Odoo Workbench projection reuses this exact
    derivation instead of a second implementation.

    Precedence is the Vendor Bill builder's own (``app.billing.builder``):

    1. Whole-invoice operating-expense mode -- a MATCHED ``operating_expense_match``
       with a positive account on a product-identifier-free invoice. Execution books
       *every* line to that one account and ignores per-line account-only decisions;
       product-backed lines cannot coexist with it (its product results must be
       INVALID_INPUT). Reported as ``accounting_resolution`` when the match comes from
       an accepted review-scoped accounting resolution, else
       ``operating_expense_mapping``.
    2. Otherwise, per line: an explicit account-only decision, else a matched product
       (human-selected or automatic), else unresolved.
    """

    invoice_account = _invoice_level_expense_account(source)
    if invoice_account is not None:
        match = source.operating_expense_match
        kind = (
            EffectiveLineResolutionKind.ACCOUNTING_RESOLUTION
            if match.matched_by == REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY
            else EffectiveLineResolutionKind.OPERATING_EXPENSE_MAPPING
        )
        return {
            line.line_number: EffectiveLineResolution(
                kind=kind,
                product_id=None,
                product_source=None,
                matched_by=match.matched_by,
                match_status=match.status.value,
                expense_account_id=invoice_account,
            )
            for line in source.invoice.lines
        }

    account_only = {
        resolution.line_number: resolution.expense_account_id
        for resolution in source.line_resolutions
        if resolution.account_only
    }
    resolutions: dict[str | None, EffectiveLineResolution] = {}
    for line_result in source.product_match.line_results:
        result = line_result.result
        if line_result.line_number in account_only:
            resolutions[line_result.line_number] = EffectiveLineResolution(
                kind=EffectiveLineResolutionKind.ACCOUNT_ONLY,
                product_id=None,
                product_source=None,
                matched_by=None,
                match_status=result.status.value,
                expense_account_id=account_only[line_result.line_number],
            )
        elif result.status is ProductMatchStatus.MATCHED and result.product_id is not None:
            resolutions[line_result.line_number] = EffectiveLineResolution(
                kind=EffectiveLineResolutionKind.PRODUCT,
                product_id=result.product_id,
                product_source=(
                    EffectiveProductSource.HUMAN_SELECTED
                    if result.matched_by == HUMAN_SELECTED_MATCHED_BY
                    else EffectiveProductSource.AUTOMATIC
                ),
                matched_by=result.matched_by,
                match_status=result.status.value,
                expense_account_id=None,
            )
        else:
            resolutions[line_result.line_number] = EffectiveLineResolution(
                kind=EffectiveLineResolutionKind.UNRESOLVED,
                product_id=None,
                product_source=None,
                matched_by=result.matched_by,
                match_status=result.status.value,
                expense_account_id=None,
            )
    return resolutions


def _invoice_level_expense_account(source: ExecutionSourceInvoice) -> int | None:
    """The whole-invoice expense account execution uses, or ``None``.

    Exactly the builder's ``_operating_expense_mode`` predicate (pinned equal by a
    parity test): MATCHED status, a positive integer account, and an invoice whose
    every line is free of deterministic product identifiers.
    """

    match = source.operating_expense_match
    if match is None or match.status is not OperatingExpenseMatchStatus.MATCHED:
        return None
    account_id = match.expense_account_id
    if type(account_id) is not int or account_id <= 0:
        return None
    if not invoice_is_product_identifier_free(source.invoice):
        return None
    return account_id
