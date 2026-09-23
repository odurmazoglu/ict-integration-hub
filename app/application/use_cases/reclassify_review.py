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
        execution_evidence = build_review_execution_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            invoice=source.invoice,
            decision_result=self._execution_decision_result(decision_result, effect),
        )
        matched_rule_code = classification_evidence.matched_rule_code if classification_evidence is not None else None
        matched_rule_id = classification_evidence.matched_rule_id if classification_evidence is not None else None

        recomputed_operating_expense_match = self._effective_operating_expense_match(
            decision_result, effect, invoice=source.invoice, command=command
        )
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
        self, decision_result: DecisionResult, effect: _AcceptedRemediationEffect | None
    ) -> DecisionResult:
        """P0-PROD-10D: substitute a MATCHED partner for *execution evidence only*.

        Used exclusively as the input to ``build_review_execution_evidence`` above --
        never for ``new_workflow``/``new_review_reasons``/classification evidence,
        which stay driven by the raw deterministic ``decision_result`` unchanged (see
        ``_effective_manual_review_reasons``/``_effective_workflow`` for the one
        narrow, MATCH_EXISTING-specific exception to that). This keeps the review's
        own displayed reasons an honest record of what the generic, active-only
        matcher actually found, while still letting a review with a genuine,
        durable, review-scoped remediation effect reach a submittable decision.
        Fires only when:

        * an accepted ``SupplierRemediationEffect`` exists for this *exact*
          ``(review_id, company_id)`` -- never any other review's or company's
          effect, and never a generic broadened search of inactive partners, and
        * the raw deterministic partner match is not already MATCHED (a normal
          active-partner review is completely unaffected -- this never runs for it
          because there is nothing to substitute).

        Product matching is untouched entirely: an unresolved product line is
        already correctly handled by the existing decision-time
        ``LineResolution.selected_product_id`` / ``apply_selected_product_resolutions``
        override, which needs no involvement here.
        """

        if effect is None:
            return decision_result
        partner_match = decision_result.partner_match
        if partner_match is not None and partner_match.status is PartnerMatchStatus.MATCHED:
            return decision_result
        return replace(decision_result, partner_match=_synthesized_matched_partner(effect))

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
    """P0-PROD-15N/15P: fold an accepted MATCH_EXISTING remediation into the
    review's own effective classification reasons.

    An accepted ``MATCH_EXISTING`` effect is the operator's authoritative
    adjudication of which of the raw matcher's valid, active, exact-VAT
    candidates is the correct supplier for this review -- it does not claim
    only one candidate exists in Odoo, and it never mutates Odoo, so the raw
    matcher legitimately keeps finding the same ambiguity forever. Only two
    things are ever removed here, and only when there is an accepted
    ``MATCH_EXISTING`` effect for this exact ``(review_id, company_id)`` to
    justify it:

    * ``SUPPLIER_AMBIGUOUS``, unconditionally (P0-PROD-15N);
    * ``OPERATING_EXPENSE_MAPPING_REQUIRED``/``_AMBIGUOUS`` (P0-PROD-15P), but
      only when ``recomputed_operating_expense_match`` -- a fresh, read-only
      recomputation against the real persistent mapping table for the effect's
      resolved supplier -- itself resolves to MATCHED. A mapping that does not
      yet exist, or that is itself ambiguous, changes nothing here.

    Every other reason -- ``SUPPLIER_NOT_FOUND`` included -- passes through
    completely untouched, so this never overlaps with the P0-PROD-10D
    Stage-1-only override above, and never broadens
    ``CREATE_PERMANENT_SUPPLIER``/``ONE_OFF_VENDOR``/``USE_ONE_OFF_SUPPLIER``
    (P0-PROD-15L intentionally kept those SUPPLIER_NOT_FOUND-only).
    """

    if effect is None or effect.mode != _MATCH_EXISTING_MODE:
        return raw_reasons
    strip_codes = {ManualReviewReasonCode.SUPPLIER_AMBIGUOUS}
    if recomputed_operating_expense_match is not None and (
        recomputed_operating_expense_match.status is OperatingExpenseMatchStatus.MATCHED
    ):
        strip_codes |= _OPERATING_EXPENSE_REASON_CODES
    if not any(reason.code in strip_codes for reason in raw_reasons):
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
