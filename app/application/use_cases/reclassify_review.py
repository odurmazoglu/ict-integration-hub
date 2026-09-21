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
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine
from app.application.dto import DecisionResult
from app.application.exceptions import ApplicationError
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
from app.matching import PartnerMatchResult, PartnerMatchStatus

SAFE_RECLASSIFICATION_ERROR = "Deterministic review reclassification failed."

#: P0-PROD-10D. A synthesized partner match sourced from a durable, review-scoped
#: SupplierRemediationEffect is treated as an exact match for evidence purposes --
#: it was itself only ever recorded through the gated, narrow-authorization-
#: protected remediation write path (see PR #159/#163), never guessed.
REMEDIATION_EFFECT_MATCH_CONFIDENCE = Decimal("1.00")
REMEDIATION_EFFECT_MATCHED_BY = "supplier_remediation_effect"


class ReclassifyWorkbenchReviewUseCase:
    """Application boundary for one non-destructive review reclassification."""

    def __init__(
        self,
        *,
        decision_engine: DecisionEngine,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        reclassification_writer: ReviewReclassificationWriter,
        supplier_remediation_effect_reader: SupplierRemediationEffectWriter | None = None,
    ) -> None:
        self._decision_engine = decision_engine
        self._source_invoice_reader = source_invoice_reader
        self._reclassification_writer = reclassification_writer
        # Optional (P0-PROD-10D): only set by composition roots that want archived
        # Hub-owned ONE_OFF_VENDOR reuse to be able to reach a submittable decision.
        # None preserves byte-identical pre-10D behavior for any existing caller.
        self._supplier_remediation_effect_reader = supplier_remediation_effect_reader

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
            decision_result=self._execution_decision_result(command, decision_result),
        )
        matched_rule_code = classification_evidence.matched_rule_code if classification_evidence is not None else None
        matched_rule_id = classification_evidence.matched_rule_id if classification_evidence is not None else None

        proposal = ReviewReclassificationProposal(
            review_id=command.review_id,
            company_id=command.company_id,
            expected_version=command.expected_version,
            trigger=command.trigger,
            note=command.note,
            source_invoice_id=source.source_invoice_id,
            new_workflow=decision_result.workflow,
            new_review_reasons=decision_result.review_reasons,
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

    def _execution_decision_result(
        self, command: ReclassifyReviewCommand, decision_result: DecisionResult
    ) -> DecisionResult:
        """P0-PROD-10D: substitute a MATCHED partner for *execution evidence only*.

        Used exclusively as the input to ``build_review_execution_evidence`` above --
        never for ``new_workflow``/``new_review_reasons``/classification evidence,
        which stay driven by the raw deterministic ``decision_result`` unchanged. This
        keeps the review's own displayed reasons an honest record of what the generic,
        active-only matcher actually found, while still letting a review with a
        genuine, durable, review-scoped remediation effect reach a submittable
        decision. Fires only when:

        * a reader was actually wired in (composition opts in explicitly), and
        * the raw deterministic partner match is not already MATCHED (a normal
          active-partner review is completely unaffected -- this never runs for it
          because there is nothing to substitute), and
        * an accepted ``SupplierRemediationEffect`` exists for this *exact*
          ``(review_id, company_id)`` -- never any other review's or company's
          effect, and never a generic broadened search of inactive partners.

        Product matching is untouched entirely: an unresolved product line is
        already correctly handled by the existing decision-time
        ``LineResolution.selected_product_id`` / ``apply_selected_product_resolutions``
        override, which needs no involvement here.
        """

        if self._supplier_remediation_effect_reader is None:
            return decision_result
        partner_match = decision_result.partner_match
        if partner_match is not None and partner_match.status is PartnerMatchStatus.MATCHED:
            return decision_result
        effect = self._supplier_remediation_effect_reader.find_latest_remediation_effect(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        if effect is None:
            return decision_result
        return replace(
            decision_result,
            partner_match=PartnerMatchResult(
                status=PartnerMatchStatus.MATCHED,
                partner_id=effect.resolved_partner_id,
                matched_by=REMEDIATION_EFFECT_MATCHED_BY,
                reason="Resolved via an accepted supplier remediation effect for this review.",
                candidate_count=1,
                confidence=REMEDIATION_EFFECT_MATCH_CONFIDENCE,
            ),
        )


def _reclassification_idempotency_key(command: ReclassifyReviewCommand) -> str:
    # The recommendation/manual-review strategies never persist or write this key;
    # it exists only to satisfy the ImportInvoiceCommand contract.
    return f"reclassify:{command.company_id}:{command.review_id}:{command.expected_version}"
