"""Non-destructive deterministic reclassification of an existing Workbench review.

Reclassification loads the review's immutable ``ReviewSourceInvoiceEvidence``,
reruns the *same* normal ``DecisionEngine`` used by first-time import against
current master data, and advances the review projection to version N+1 while
preserving the previous state in an immutable ``WorkbenchReviewReclassification``
event. It is not a human decision, performs no ERP write, and never re-fetches
Uyumsoft or accepts an invoice from the caller.

P0-PROD-10D: when the generic deterministic partner match fails (as it always
will for an archived, Hub-owned ONE_OFF_VENDOR partner -- the matcher correctly
never considers inactive partners, see ``PartnerMatchingEngine``), this use case
consults the review's own persisted, accepted ``SupplierRemediationEffect`` --
never the generic matcher, never any other review's or company's effect -- and,
only when one exists, substitutes a synthesized ``MATCHED`` result for Stage-1
*execution* evidence construction only. The raw deterministic outcome still
drives ``new_workflow``/``new_review_reasons``/classification evidence
unchanged, so a review with no accepted remediation effect (including any
non-Hub-owned inactive partner) is completely unaffected -- see
``_execution_decision_result`` below.

P0-PROD-15N: an accepted ``MATCH_EXISTING`` ``SupplierRemediationEffect`` is a
different shape of the same idea. Unlike ``CREATE_PERMANENT_SUPPLIER``/
``ONE_OFF_VENDOR`` (which write an Odoo partner that the raw matcher then finds
naturally on its own), ``MATCH_EXISTING`` never mutates Odoo -- both exact-VAT
candidates the raw ``PartnerMatchingEngine`` sees remain exactly as ambiguous
after the remediation as before it, so reclassification would otherwise
reproduce ``SUPPLIER_AMBIGUOUS`` forever, no matter how deterministic the
operator's selection was (proven against real production: PR #175 made the
selection acceptable, but the review's own reasons never actually cleared).
The operator's accepted selection is a higher-level adjudication of *which* of
the raw matcher's valid candidates is correct -- not a claim that only one
candidate exists. So here, unlike the Stage-1-only P0-PROD-10D override, the
effect *does* drive the review's own effective ``new_workflow``/
``new_review_reasons`` -- see ``_effective_manual_review_reasons`` and
``_effective_workflow`` below -- while the raw ``classification_evidence``
(built from the untouched ``decision_result``) stays a truthful record of what
the generic matcher actually found. This only ever strips
``SUPPLIER_AMBIGUOUS``; every other reason (``SUPPLIER_NOT_FOUND`` included)
is completely unaffected, and the effect lookup is scoped to exactly this
``(review_id, company_id)`` exactly like the Stage-1 override.

P0-PROD-15P: the same raw-matcher-stays-stuck problem also blocks operating-
expense classification for a MATCH_EXISTING-remediated, still-raw-ambiguous
review. ``OperatingExpenseMatchingEngine.match_invoice`` (see
``app.application.expense_mapping.matching``) itself requires
``partner_match.status is MATCHED`` on the *raw* partner match it is handed --
inherited from the rule engine's own internal call, never the effect-aware
substitution -- so even once a real ``OperatingExpenseMapping`` row exists for
the effect's resolved supplier, reclassification would otherwise keep
reporting OPERATING_EXPENSE_MAPPING_REQUIRED forever, for the exact same
structural reason SUPPLIER_AMBIGUOUS would. The fix mirrors P0-PROD-15N
exactly: when wired in (``operating_expense_matcher``, optional -- ``None``
preserves byte-identical behavior for any existing caller) and only when an
effect exists and the raw partner is not already MATCHED, the *same*
synthesized MATCHED partner used for Stage-1 evidence is used to recompute
operating-expense matching, and ``_effective_manual_review_reasons`` strips
OPERATING_EXPENSE_MAPPING_REQUIRED/AMBIGUOUS only when that recomputation
itself resolves to MATCHED -- never fabricated, never applied when no real
mapping row exists.

P0-PROD-15T: a supplier-wide ``OperatingExpenseMapping`` is unsafe for a
mixed-purpose supplier (e.g. one invoice is genuinely internal-use, another
from the same supplier is resale/a customer project) -- committing either
review's account to the shared supplier-level table would silently
contaminate the other. ``ReviewAccountingResolution`` (see
``app.application.workbench.accounting_resolution``) is the review-scoped
escape hatch: an operator's explicit, immutable accounting decision for
*exactly this* review version, never written into the supplier-wide table.
When wired in (``review_accounting_resolution_reader``, optional -- ``None``
preserves byte-identical behavior for every existing caller) and an accepted
resolution exists, it takes precedence over the P0-PROD-15P supplier-wide
recomputation above -- see
``_review_accounting_resolution_operating_expense_match`` and the precedence
order in ``execute()`` -- and, unlike the P0-PROD-15N/15P override, applies
regardless of ``SupplierRemediationEffect``/mode: it is orthogonal to how (or
whether) the supplier was resolved, not a consequence of it. Exactly like
P0-PROD-15P, it only ever strips OPERATING_EXPENSE_MAPPING_REQUIRED/AMBIGUOUS,
never any other reason, and the raw ``classification_evidence`` stays an
untouched, truthful record.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine
from app.application.dto import DecisionResult
from app.application.exceptions import ApplicationError
from app.application.expense_mapping.matcher import OperatingExpenseMatcher
from app.application.expense_mapping.matching import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.application.use_cases.review_classification_outcome import (
    build_review_classification_evidence,
    build_review_execution_evidence,
)
from app.application.workbench.exceptions import ReviewPersistenceError, WorkbenchContractError
from app.application.workbench.ports import (
    ReviewAccountingResolutionReader,
    ReviewReclassificationWriter,
    ReviewSourceInvoiceEvidenceReader,
    SupplierRemediationEffectWriter,
)
from app.application.workbench.reclassification import (
    ReclassifyReviewCommand,
    ReviewReclassificationProposal,
    ReviewReclassificationResult,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.matching import PartnerMatchResult, PartnerMatchStatus

SAFE_RECLASSIFICATION_ERROR = "Deterministic review reclassification failed."

#: P0-PROD-10D. A synthesized partner match sourced from a durable, review-scoped
#: SupplierRemediationEffect is treated as an exact match for evidence purposes --
#: it was itself only ever recorded through the gated, narrow-authorization-
#: protected remediation write path (see PR #159/#163), never guessed.
REMEDIATION_EFFECT_MATCH_CONFIDENCE = Decimal("1.00")
REMEDIATION_EFFECT_MATCHED_BY = "supplier_remediation_effect"

#: P0-PROD-15N. The exact string value of ``SupplierResolutionMode.MATCH_EXISTING``
#: (a ``StrEnum``, so an instance compares equal to its own value). Compared against
#: as a plain string, never the enum itself, so this module never imports
#: ``app.application.workbench.supplier_resolution`` (see
#: ``test_vendor_bill_and_classification_paths_do_not_import_supplier_resolution``).
_MATCH_EXISTING_MODE = "match_existing"

#: P0-PROD-15T. The exact string value of ``AccountingTreatmentType.EXPENSE_ACCOUNT``
#: (a ``StrEnum``). Compared as a plain string for the same import-isolation reason
#: as ``_MATCH_EXISTING_MODE`` -- this module never imports
#: ``app.application.workbench.accounting_resolution``.
_EXPENSE_ACCOUNT_TREATMENT = "expense_account"

REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY = "review_accounting_resolution"


class _AcceptedRemediationEffect(Protocol):
    """Structural type for ``SupplierRemediationEffect``.

    Kept structural, exactly like ``SupplierReclassifier``/``WorkbenchReviewRepublisher``
    below, so this module -- which must never gain the ability to reach the supplier
    partner writer -- never imports ``app.application.workbench.supplier_remediation``
    (see ``test_import_and_reclassification_never_reach_the_supplier_writer``). Only the
    two fields this module actually reads are declared, and ``mode`` is typed as the
    plain ``str`` a ``SupplierResolutionMode`` (a ``StrEnum``) compares equal to, for
    the same import-isolation reason as ``_MATCH_EXISTING_MODE`` above.
    """

    mode: str
    resolved_partner_id: int


class _AcceptedAccountingResolution(Protocol):
    """Structural type for ``ReviewAccountingResolution`` (P0-PROD-15T).

    Kept structural for the same import-isolation reason as
    ``_AcceptedRemediationEffect`` -- this module never imports
    ``app.application.workbench.accounting_resolution``. ``id`` is the
    resolution's own persisted row id, reused as ``OperatingExpenseMatchResult
    .mapping_id`` below (there is no real ``OperatingExpenseMapping`` row for a
    review-scoped resolution, but a positive, traceable id is still required by
    ``operating_expense_evidence_errors``).
    """

    id: int
    treatment_type: str
    expense_account_id: int
    expense_category: str


class ReclassifyWorkbenchReviewUseCase:
    """Application boundary for one non-destructive review reclassification."""

    def __init__(
        self,
        *,
        decision_engine: DecisionEngine,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        reclassification_writer: ReviewReclassificationWriter,
        supplier_remediation_effect_reader: SupplierRemediationEffectWriter | None = None,
        operating_expense_matcher: OperatingExpenseMatcher | None = None,
        review_accounting_resolution_reader: ReviewAccountingResolutionReader | None = None,
    ) -> None:
        self._decision_engine = decision_engine
        self._source_invoice_reader = source_invoice_reader
        self._reclassification_writer = reclassification_writer
        # Optional (P0-PROD-10D): only set by composition roots that want archived
        # Hub-owned ONE_OFF_VENDOR reuse to be able to reach a submittable decision.
        # None preserves byte-identical pre-10D behavior for any existing caller.
        self._supplier_remediation_effect_reader = supplier_remediation_effect_reader
        # Optional (P0-PROD-15P): only set by composition roots that want a
        # MATCH_EXISTING-remediated (raw-ambiguous-forever) review to be able to
        # reach OPERATING_EXPENSE_MAPPING_REQUIRED resolution once a real mapping
        # exists. None preserves byte-identical pre-15P behavior for any existing
        # caller. Should be the SAME matcher/repository instance the production
        # DecisionEngine's rule engine uses, so "would this now match" is asked of
        # the identical persistent mapping table -- never a second, divergent one.
        self._operating_expense_matcher = operating_expense_matcher
        # Optional (P0-PROD-15T): only set by composition roots that want a
        # review-scoped ReviewAccountingResolution to be consulted at all. None
        # preserves byte-identical pre-15T behavior for every existing caller.
        self._review_accounting_resolution_reader = review_accounting_resolution_reader

    async def execute(self, command: ReclassifyReviewCommand) -> ReviewReclassificationResult:
        if not isinstance(command, ReclassifyReviewCommand):
            raise WorkbenchContractError("ReclassifyReviewCommand is required.")

        # Source of truth: the immutable snapshot only. Never Uyumsoft, never the caller.
        source = self._source_invoice_reader.get(
            review_id=command.review_id,
            company_id=command.company_id,
        )

        decision_result = await self._decide(
            ImportInvoiceCommand(
                invoice=source.invoice,
                idempotency_key=_reclassification_idempotency_key(command),
                company_id=command.company_id,
            )
        )

        # Looked up at most once and reused by both the Stage-1 execution-evidence
        # override (P0-PROD-10D) and the effective-classification override
        # (P0-PROD-15N) below, so both consult the exact same (review_id,
        # company_id)-scoped effect. Skipped entirely (same laziness as before
        # P0-PROD-15N) once the raw partner match already succeeded on its own --
        # SUPPLIER_AMBIGUOUS cannot co-occur with a MATCHED partner, so there is
        # nothing either override could ever substitute in that case.
        raw_partner_match = decision_result.partner_match
        effect = (
            None
            if raw_partner_match is not None and raw_partner_match.status is PartnerMatchStatus.MATCHED
            else self._find_latest_remediation_effect(command)
        )

        to_version = command.expected_version + 1
        classification_evidence = build_review_classification_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            decision_result=decision_result,
        )

        # P0-PROD-15T precedence: a review-scoped ReviewAccountingResolution (this
        # exact review only) always wins over the P0-PROD-15P supplier-wide-effect
        # recomputation -- see module docstring. `or` is exact here: both return
        # a real dataclass instance or None, and a real instance is always truthy.
        # Computed once, here, and reused for both effective execution evidence
        # (P0-PROD-15X) and effective reasons/workflow below -- one authoritative
        # effective result, never independently recomputed in another layer.
        recomputed_operating_expense_match = self._review_accounting_resolution_operating_expense_match(
            decision_result, effect, command=command
        ) or self._effective_operating_expense_match(decision_result, effect, invoice=source.invoice, command=command)

        execution_evidence = build_review_execution_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            invoice=source.invoice,
            decision_result=self._execution_decision_result(
                decision_result, effect, recomputed_operating_expense_match
            ),
        )
        matched_rule_code = classification_evidence.matched_rule_code if classification_evidence is not None else None
        matched_rule_id = classification_evidence.matched_rule_id if classification_evidence is not None else None

        effective_review_reasons = _effective_manual_review_reasons(
            decision_result.review_reasons, effect, recomputed_operating_expense_match
        )
        effective_workflow = _effective_workflow(decision_result.workflow, effective_review_reasons)

        proposal = ReviewReclassificationProposal(
            review_id=command.review_id,
            company_id=command.company_id,
            expected_version=command.expected_version,
            trigger=command.trigger,
            note=command.note,
            source_invoice_id=source.source_invoice_id,
            new_workflow=effective_workflow,
            new_review_reasons=effective_review_reasons,
            new_warnings=decision_result.warnings,
            matched_rule_code=matched_rule_code,
            matched_rule_id=matched_rule_id,
            new_classification_evidence=classification_evidence,
            new_execution_evidence=execution_evidence,
        )
        return self._reclassification_writer.reclassify_review(proposal)

    async def _decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        try:
            return await self._decision_engine.decide(command)
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe application error
            raise ReviewPersistenceError(SAFE_RECLASSIFICATION_ERROR) from exc

    def _find_latest_remediation_effect(self, command: ReclassifyReviewCommand) -> _AcceptedRemediationEffect | None:
        if self._supplier_remediation_effect_reader is None:
            return None
        return self._supplier_remediation_effect_reader.find_latest_remediation_effect(
            review_id=command.review_id,
            company_id=command.company_id,
        )

    def _execution_decision_result(
        self,
        decision_result: DecisionResult,
        effect: _AcceptedRemediationEffect | None,
        recomputed_operating_expense_match: OperatingExpenseMatchResult | None,
    ) -> DecisionResult:
        """Substitute effective, already-authoritative facts for *execution evidence
        only* -- never for ``new_workflow``/``new_review_reasons``/classification
        evidence, which stay driven by the raw deterministic ``decision_result``
        unchanged (see ``_effective_manual_review_reasons``/``_effective_workflow``
        for the one narrow, MATCH_EXISTING-specific exception to that). This keeps
        the review's own displayed reasons an honest record of what the generic
        matcher actually found, while still letting a review with a genuine,
        durable, review-scoped effect/resolution reach a submittable decision.

        Two independent substitutions, each gated on its own accepted-state
        precondition:

        * P0-PROD-10D: partner match, substituted only when an accepted
          ``SupplierRemediationEffect`` exists for this *exact*
          ``(review_id, company_id)`` -- never any other review's or company's
          effect, and never a generic broadened search of inactive partners --
          and the raw deterministic partner match is not already MATCHED (a
          normal active-partner review is completely unaffected).
        * P0-PROD-15X: operating-expense match, substituted with
          ``recomputed_operating_expense_match`` whenever the caller has computed
          one (already gated, by the caller, to require an accepted P0-PROD-15T
          ``ReviewAccountingResolution`` or a P0-PROD-15P supplier-wide-effect
          recomputation matching -- never fabricated here). Fixes the gap where
          the review's effective reasons/workflow already treated the review as
          operating-expense-resolved, but Stage-1 execution evidence still saw
          only the raw, unmatched result, so no ``WorkbenchReviewExecutionEvidence``
          was ever persisted and decision submission failed with
          ``execution_source_invoice_not_found``.

        Product matching is untouched entirely: an unresolved product line is
        already correctly handled by the existing decision-time
        ``LineResolution.selected_product_id`` / ``apply_selected_product_resolutions``
        override, which needs no involvement here.
        """

        result = decision_result
        if effect is not None:
            partner_match = result.partner_match
            if partner_match is None or partner_match.status is not PartnerMatchStatus.MATCHED:
                result = replace(result, partner_match=_synthesized_matched_partner(effect))
        if recomputed_operating_expense_match is not None:
            result = replace(result, operating_expense_match=recomputed_operating_expense_match)
        return result

    def _effective_operating_expense_match(
        self,
        decision_result: DecisionResult,
        effect: _AcceptedRemediationEffect | None,
        *,
        invoice: object,
        command: ReclassifyReviewCommand,
    ) -> OperatingExpenseMatchResult | None:
        """P0-PROD-15P: ask "would operating-expense matching succeed once the
        effect's resolved supplier is treated as matched?" -- read-only, using the
        exact same persistent mapping table the production rule engine itself
        queries. Returns ``None`` (no override) unless a matcher was wired in, an
        effect exists, and the raw partner match is not already MATCHED -- the
        identical gate ``_execution_decision_result`` already uses, so this never
        fires for a normal active-partner review either.
        """

        if self._operating_expense_matcher is None or effect is None or effect.mode != _MATCH_EXISTING_MODE:
            return None
        partner_match = decision_result.partner_match
        if partner_match is not None and partner_match.status is PartnerMatchStatus.MATCHED:
            return None
        return self._operating_expense_matcher.match_invoice(
            invoice,
            company_id=command.company_id,
            partner_match=_synthesized_matched_partner(effect),
        )

    def _review_accounting_resolution_operating_expense_match(
        self,
        decision_result: DecisionResult,
        effect: _AcceptedRemediationEffect | None,
        *,
        command: ReclassifyReviewCommand,
    ) -> OperatingExpenseMatchResult | None:
        """P0-PROD-15T: the highest-precedence operating-expense override.

        Unlike ``_effective_operating_expense_match`` above, this is not gated on
        ``effect.mode`` at all -- a review-scoped accounting resolution is orthogonal
        to *how* (or whether) the supplier was resolved. Returns ``None`` (no
        override) unless a reader was wired in, the raw operating-expense match is
        not already MATCHED (nothing to override), an accepted resolution exists for
        this exact ``(review_id, company_id)``, its ``treatment_type`` is the one
        supported today (``EXPENSE_ACCOUNT``), and a resolvable supplier partner id
        is available -- from the raw match if already MATCHED, else from ``effect``
        (mirrors ``CreateNewProductUseCase``/``SubmitOperatingExpenseMappingUseCase``:
        a review whose supplier was never resolved by any means has nothing to pin
        this override's evidence to, so it fails closed to ``None`` rather than
        guessing a partner id).
        """

        if self._review_accounting_resolution_reader is None:
            return None
        raw_operating_expense_match = decision_result.operating_expense_match
        if raw_operating_expense_match is not None and raw_operating_expense_match.status is (
            OperatingExpenseMatchStatus.MATCHED
        ):
            return None
        resolution = self._review_accounting_resolution_reader.find_latest_accounting_resolution(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        if resolution is None or resolution.treatment_type != _EXPENSE_ACCOUNT_TREATMENT:
            return None
        raw_partner_match = decision_result.partner_match
        if raw_partner_match is not None and raw_partner_match.status is PartnerMatchStatus.MATCHED:
            vendor_partner_id = raw_partner_match.partner_id
        elif effect is not None:
            vendor_partner_id = effect.resolved_partner_id
        else:
            return None
        return OperatingExpenseMatchResult(
            status=OperatingExpenseMatchStatus.MATCHED,
            reason="Resolved via an accepted review-scoped accounting resolution for this review.",
            candidate_count=1,
            mapping_id=resolution.id,
            company_id=command.company_id,
            vendor_partner_id=vendor_partner_id,
            expense_account_id=resolution.expense_account_id,
            expense_category=resolution.expense_category,
            matched_by=REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY,
            confidence=REMEDIATION_EFFECT_MATCH_CONFIDENCE,
        )


#: Reason codes an accepted MATCH_EXISTING effect may ever strip from the review's
#: own effective reasons -- SUPPLIER_AMBIGUOUS unconditionally (P0-PROD-15N), and
#: the operating-expense codes only when a fresh, real recomputation against the
#: persistent mapping table itself resolves to MATCHED (P0-PROD-15P). Every other
#: reason, SUPPLIER_NOT_FOUND included, is never in this set.
_OPERATING_EXPENSE_REASON_CODES = frozenset(
    {
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS,
    }
)


def _synthesized_matched_partner(effect: _AcceptedRemediationEffect) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=effect.resolved_partner_id,
        matched_by=REMEDIATION_EFFECT_MATCHED_BY,
        reason="Resolved via an accepted supplier remediation effect for this review.",
        candidate_count=1,
        confidence=REMEDIATION_EFFECT_MATCH_CONFIDENCE,
    )


def _effective_manual_review_reasons(
    raw_reasons: tuple[ManualReviewReason, ...],
    effect: _AcceptedRemediationEffect | None,
    recomputed_operating_expense_match: OperatingExpenseMatchResult | None,
) -> tuple[ManualReviewReason, ...]:
    """P0-PROD-15N/15P/15T: fold accepted review-scoped remediation/resolution
    state into the review's own effective classification reasons.

    Two completely independent strip decisions, each with its own accepted-state
    precondition:

    * ``SUPPLIER_AMBIGUOUS`` is stripped only when there is an accepted
      ``MATCH_EXISTING`` ``SupplierRemediationEffect`` for this exact
      ``(review_id, company_id)`` (P0-PROD-15N) -- the operator's authoritative
      adjudication of which of the raw matcher's valid, active, exact-VAT
      candidates is correct. It never claims only one candidate exists in Odoo,
      and never mutates Odoo, so the raw matcher legitimately keeps finding the
      same ambiguity forever.
    * ``OPERATING_EXPENSE_MAPPING_REQUIRED``/``_AMBIGUOUS`` is stripped only
      when ``recomputed_operating_expense_match`` -- already computed by the
      caller with P0-PROD-15T's review-scoped-resolution-first, then
      P0-PROD-15P's supplier-wide-effect-recomputation, precedence -- itself
      resolves to MATCHED. This is deliberately **not** gated on ``effect``/its
      mode at all: a review-scoped ``ReviewAccountingResolution`` (P0-PROD-15T)
      is orthogonal to *how*, or whether, the supplier was resolved, unlike the
      P0-PROD-15P supplier-wide-effect recomputation it can take precedence
      over (which *is* still gated to ``effect.mode == MATCH_EXISTING`` inside
      ``_effective_operating_expense_match``, so existing P0-PROD-15N/15P
      behavior for that source is completely unchanged).

    Every other reason -- ``SUPPLIER_NOT_FOUND`` included -- passes through
    completely untouched, so this never overlaps with the P0-PROD-10D
    Stage-1-only override above, and never broadens
    ``CREATE_PERMANENT_SUPPLIER``/``ONE_OFF_VENDOR``/``USE_ONE_OFF_SUPPLIER``
    (P0-PROD-15L intentionally kept those SUPPLIER_NOT_FOUND-only).
    """

    strip_codes: set[ManualReviewReasonCode] = set()
    if effect is not None and effect.mode == _MATCH_EXISTING_MODE:
        strip_codes.add(ManualReviewReasonCode.SUPPLIER_AMBIGUOUS)
    if recomputed_operating_expense_match is not None and (
        recomputed_operating_expense_match.status is OperatingExpenseMatchStatus.MATCHED
    ):
        strip_codes |= _OPERATING_EXPENSE_REASON_CODES
    if not strip_codes or not any(reason.code in strip_codes for reason in raw_reasons):
        return raw_reasons
    return tuple(reason for reason in raw_reasons if reason.code not in strip_codes)


def _effective_workflow(
    raw_workflow: WorkflowType,
    effective_reasons: tuple[ManualReviewReason, ...],
) -> WorkflowType:
    """Re-derive the workflow from the effective reasons using the same rule the
    deterministic rule engine itself uses (``app.application.rules.deterministic``):
    any Manual Review reason forces ``MANUAL_REVIEW``; none of the rule engine's
    manual-review branches survive to any other workflow than ``VENDOR_BILL``.
    A no-op unless ``raw_workflow`` was already ``MANUAL_REVIEW`` and the
    P0-PROD-15N override above actually emptied the reasons.
    """

    if raw_workflow is not WorkflowType.MANUAL_REVIEW:
        return raw_workflow
    if effective_reasons:
        return raw_workflow
    return WorkflowType.VENDOR_BILL


def _reclassification_idempotency_key(command: ReclassifyReviewCommand) -> str:
    # The recommendation/manual-review strategies never persist or write this key;
    # it exists only to satisfy the ImportInvoiceCommand contract.
    return f"reclassify:{command.company_id}:{command.review_id}:{command.expected_version}"
