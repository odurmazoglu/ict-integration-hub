"""The one authoritative "raw decision + accepted review-scoped effects -> effective
decision" computation, shared by :class:`~app.application.use_cases.reclassify_review.
ReclassifyWorkbenchReviewUseCase` and
:class:`~app.application.workbench.execution_evidence_recovery_use_cases.
RebuildReviewExecutionEvidenceUseCase` (P0-PROD-15Z).

Extracted from ``reclassify_review.py`` verbatim (P0-PROD-10D/15N/15P/15T logic
unchanged) so reclassification and execution-evidence recovery can never drift into
two subtly different algorithms for "what does this review's classification
effectively resolve to right now" -- there is exactly one implementation, reused by
both callers. See ``reclassify_review.py``'s module docstring for the full history
and rationale of each override this resolves.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine
from app.application.dto import DecisionResult
from app.application.exceptions import ApplicationError
from app.application.expense_mapping.matcher import OperatingExpenseMatcher
from app.application.expense_mapping.matching import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import ReviewPersistenceError
from app.application.workbench.ports import (
    ReviewAccountingResolutionReader,
    ReviewSourceInvoiceEvidenceReader,
    SupplierRemediationEffectWriter,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.matching import PartnerMatchResult, PartnerMatchStatus

SAFE_EFFECTIVE_DECISION_ERROR = "Deterministic review classification failed."

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

#: Reason codes an accepted MATCH_EXISTING effect or a resolved operating-expense
#: match may ever strip from the review's own effective reasons -- SUPPLIER_AMBIGUOUS
#: unconditionally (P0-PROD-15N), and the operating-expense codes only when a fresh,
#: real recomputation resolves to MATCHED (P0-PROD-15P/15T). Every other reason,
#: SUPPLIER_NOT_FOUND included, is never in this set.
_OPERATING_EXPENSE_REASON_CODES = frozenset(
    {
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS,
    }
)


class AcceptedRemediationEffect(Protocol):
    """Structural type for ``SupplierRemediationEffect``.

    Kept structural, exactly like the analogous Protocols below, so this module --
    which must never gain the ability to reach the supplier partner writer -- never
    imports ``app.application.workbench.supplier_remediation`` (see
    ``test_import_and_reclassification_never_reach_the_supplier_writer``). Only the
    two fields this module actually reads are declared, and ``mode`` is typed as the
    plain ``str`` a ``SupplierResolutionMode`` (a ``StrEnum``) compares equal to, for
    the same import-isolation reason as ``_MATCH_EXISTING_MODE`` above.
    """

    mode: str
    resolved_partner_id: int


class AcceptedAccountingResolution(Protocol):
    """Structural type for ``ReviewAccountingResolution`` (P0-PROD-15T).

    Kept structural for the same import-isolation reason as
    ``AcceptedRemediationEffect`` -- this module never imports
    ``app.application.workbench.accounting_resolution``. ``id`` is the resolution's
    own persisted row id, reused as ``OperatingExpenseMatchResult.mapping_id`` below
    (there is no real ``OperatingExpenseMapping`` row for a review-scoped resolution,
    but a positive, traceable id is still required by ``operating_expense_evidence_errors``).
    """

    id: int
    treatment_type: str
    expense_account_id: int
    expense_category: str


@dataclass(frozen=True, slots=True)
class EffectiveDecision:
    """The complete result of resolving one review's current effective classification.

    ``decision_result`` is the untouched raw deterministic outcome -- the only input
    ever used for classification evidence, so that evidence stays a truthful record
    of what the generic matcher actually found. ``execution_decision_result`` is the
    *effective* result (accepted review-scoped effects/resolutions substituted in),
    used only as input to ``build_review_execution_evidence``.
    """

    source: ReviewSourceInvoiceEvidence
    decision_result: DecisionResult
    execution_decision_result: DecisionResult
    effective_review_reasons: tuple[ManualReviewReason, ...]
    effective_workflow: WorkflowType


class EffectiveDecisionResolver:
    """Computes :class:`EffectiveDecision` for one review, from its immutable source
    evidence and whatever accepted review-scoped effects/resolutions exist today.

    Never writes anything -- purely a read/recompute step. Callers decide what, if
    anything, to persist from the result (a version-advancing reclassification, or a
    same-version execution-evidence recovery).
    """

    def __init__(
        self,
        *,
        decision_engine: DecisionEngine,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        supplier_remediation_effect_reader: SupplierRemediationEffectWriter | None = None,
        operating_expense_matcher: OperatingExpenseMatcher | None = None,
        review_accounting_resolution_reader: ReviewAccountingResolutionReader | None = None,
    ) -> None:
        self._decision_engine = decision_engine
        self._source_invoice_reader = source_invoice_reader
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

    async def resolve(
        self,
        *,
        review_id: str,
        company_id: int,
        idempotency_key: str,
    ) -> EffectiveDecision:
        # Source of truth: the immutable snapshot only. Never Uyumsoft, never the caller.
        source = self._source_invoice_reader.get(review_id=review_id, company_id=company_id)

        decision_result = await self._decide(
            ImportInvoiceCommand(
                invoice=source.invoice,
                idempotency_key=idempotency_key,
                company_id=company_id,
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
            else self._find_latest_remediation_effect(review_id=review_id, company_id=company_id)
        )

        # P0-PROD-15T precedence: a review-scoped ReviewAccountingResolution (this
        # exact review only) always wins over the P0-PROD-15P supplier-wide-effect
        # recomputation -- see reclassify_review.py's module docstring. `or` is exact
        # here: both return a real dataclass instance or None, and a real instance is
        # always truthy.
        recomputed_operating_expense_match = self._review_accounting_resolution_operating_expense_match(
            decision_result, effect, review_id=review_id, company_id=company_id
        ) or self._effective_operating_expense_match(
            decision_result, effect, invoice=source.invoice, company_id=company_id
        )

        execution_decision_result = self._execution_decision_result(
            decision_result, effect, recomputed_operating_expense_match
        )
        effective_review_reasons = _effective_manual_review_reasons(
            decision_result.review_reasons, effect, recomputed_operating_expense_match
        )
        effective_workflow = _effective_workflow(decision_result.workflow, effective_review_reasons)

        return EffectiveDecision(
            source=source,
            decision_result=decision_result,
            execution_decision_result=execution_decision_result,
            effective_review_reasons=effective_review_reasons,
            effective_workflow=effective_workflow,
        )

    async def _decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        try:
            return await self._decision_engine.decide(command)
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe application error
            raise ReviewPersistenceError(SAFE_EFFECTIVE_DECISION_ERROR) from exc

    def _find_latest_remediation_effect(self, *, review_id: str, company_id: int) -> AcceptedRemediationEffect | None:
        if self._supplier_remediation_effect_reader is None:
            return None
        return self._supplier_remediation_effect_reader.find_latest_remediation_effect(
            review_id=review_id,
            company_id=company_id,
        )

    def _execution_decision_result(
        self,
        decision_result: DecisionResult,
        effect: AcceptedRemediationEffect | None,
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
          recomputation matching -- never fabricated here).

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
        effect: AcceptedRemediationEffect | None,
        *,
        invoice: object,
        company_id: int,
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
            company_id=company_id,
            partner_match=_synthesized_matched_partner(effect),
        )

    def _review_accounting_resolution_operating_expense_match(
        self,
        decision_result: DecisionResult,
        effect: AcceptedRemediationEffect | None,
        *,
        review_id: str,
        company_id: int,
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
        (a review whose supplier was never resolved by any means has nothing to pin
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
            review_id=review_id,
            company_id=company_id,
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
            company_id=company_id,
            vendor_partner_id=vendor_partner_id,
            expense_account_id=resolution.expense_account_id,
            expense_category=resolution.expense_category,
            matched_by=REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY,
            confidence=REMEDIATION_EFFECT_MATCH_CONFIDENCE,
        )


def _synthesized_matched_partner(effect: AcceptedRemediationEffect) -> PartnerMatchResult:
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
    effect: AcceptedRemediationEffect | None,
    recomputed_operating_expense_match: OperatingExpenseMatchResult | None,
) -> tuple[ManualReviewReason, ...]:
    """P0-PROD-15N/15P/15T: fold accepted review-scoped remediation/resolution
    state into the review's own effective classification reasons.

    Two completely independent strip decisions, each with its own accepted-state
    precondition:

    * ``SUPPLIER_AMBIGUOUS`` is stripped only when there is an accepted
      ``MATCH_EXISTING`` ``SupplierRemediationEffect`` for this exact
      ``(review_id, company_id)`` (P0-PROD-15N).
    * ``OPERATING_EXPENSE_MAPPING_REQUIRED``/``_AMBIGUOUS`` is stripped only
      when ``recomputed_operating_expense_match`` -- already computed by the
      caller with P0-PROD-15T's review-scoped-resolution-first, then
      P0-PROD-15P's supplier-wide-effect-recomputation, precedence -- itself
      resolves to MATCHED. This is deliberately **not** gated on ``effect``/its
      mode at all.

    Every other reason -- ``SUPPLIER_NOT_FOUND`` included -- passes through
    completely untouched.
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
    """

    if raw_workflow is not WorkflowType.MANUAL_REVIEW:
        return raw_workflow
    if effective_reasons:
        return raw_workflow
    return WorkflowType.VENDOR_BILL


__all__ = [
    "REMEDIATION_EFFECT_MATCH_CONFIDENCE",
    "REMEDIATION_EFFECT_MATCHED_BY",
    "REVIEW_ACCOUNTING_RESOLUTION_MATCHED_BY",
    "SAFE_EFFECTIVE_DECISION_ERROR",
    "AcceptedAccountingResolution",
    "AcceptedRemediationEffect",
    "EffectiveDecision",
    "EffectiveDecisionResolver",
]
