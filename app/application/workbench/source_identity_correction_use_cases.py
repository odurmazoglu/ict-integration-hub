"""Audited, append-only correction of one review's source-invoice identity.

See :mod:`app.application.workbench.source_identity_correction` for the model. This
use case:

1. evaluates every precondition and reports each one (dry-run is the default and
   performs zero writes);
2. re-parses the review's own stored, hash-verified UBL with the *current* parser
   and requires the result to differ from the effective source evidence in exactly
   the requested field;
3. proves the persisted value is precisely what the defect named by the correction
   reason produced from that same document;
4. recalculates classification through the normal :class:`EffectiveDecisionResolver`
   over the corrected source (never hand-built derived values);
5. on ``apply`` commits the correction, the review update, the ``N+1``
   classification evidence and the ``SOURCE_IDENTITY_CORRECTED`` reclassification
   event in one transaction, and only *after* the commit runs the best-effort
   Workbench projection sync (a projection failure never undoes the Hub commit).

It never writes Odoo partners, account moves, decisions, authorizations or
executions.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Protocol

from app.application.services import UnitOfWork
from app.application.use_cases.effective_decision import EffectiveDecisionResolver
from app.application.use_cases.review_classification_outcome import (
    build_review_classification_evidence,
    build_review_execution_evidence,
)
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import ReviewNotFoundError, WorkbenchContractError
from app.application.workbench.ports import ReviewQueueReader
from app.application.workbench.projection_sync_contracts import ReviewProjectionSynchronizer, sync_after_commit
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReviewReclassificationProposal, ReviewReclassificationTrigger
from app.application.workbench.source_identity_correction import (
    CorrectionCheckStatus,
    CorrectionPreconditionCheck,
    CorrectReviewSourceIdentityCommand,
    ExpectedChange,
    ReviewSourceInvoiceCorrection,
    SourceIdentityCorrectionOutcome,
    SourceIdentityCorrectionReport,
    SourceInvoiceCorrectionField,
    SourceInvoiceCorrectionReason,
    apply_source_invoice_corrections,
    diff_invoice_payloads,
    read_source_invoice_field,
)
from app.domain.invoice import InternalInvoice
from app.domain.invoice.parser import legacy_supplier_tax_identifier, parse_ubl_invoice
from app.domain.invoice.party_tax_identity import is_party_tax_identifier

logger = logging.getLogger(__name__)

SOURCE_CORRECTION_NOTE = "Append-only source-invoice correction ({reason}): {field} corrected from the stored document."
_HUB_ITEMS = "workbench_review_items"


class _SourceDocument(Protocol):
    document_id: int
    storage_key: str
    content_sha256: str


class SourceCorrectionStateReader(Protocol):
    def downstream_state_counts(self, *, review_id: str) -> dict[str, dict[str, int]]:
        pass

    def find_source_documents(self, *, review_id: str, company_id: int) -> tuple[_SourceDocument, ...]:
        pass


class SourceCorrectionWriter(Protocol):
    def apply_source_invoice_correction(
        self, correction: ReviewSourceInvoiceCorrection, proposal: ReviewReclassificationProposal
    ) -> None:
        pass


class CorrectableSourceInvoiceReader(Protocol):
    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        pass

    def find_corrections(self, *, review_id: str, company_id: int) -> tuple[ReviewSourceInvoiceCorrection, ...]:
        pass


class SourceDocumentContentReader(Protocol):
    def read(self, storage_key: str) -> bytes:
        pass


ResolverFactory = Callable[[CorrectableSourceInvoiceReader], EffectiveDecisionResolver]


class _PendingCorrectionSourceReader:
    """The normal effective reader plus one not-yet-persisted correction overlaid.

    Lets the unchanged :class:`EffectiveDecisionResolver` classify exactly the source
    the review will have once the correction commits.
    """

    def __init__(self, base: CorrectableSourceInvoiceReader, pending: ReviewSourceInvoiceCorrection) -> None:
        self._base = base
        self._pending = pending

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        evidence = self._base.get(review_id=review_id, company_id=company_id)
        if review_id != self._pending.review_id or company_id != self._pending.company_id:
            return evidence
        return replace(evidence, invoice=apply_source_invoice_corrections(evidence.invoice, [self._pending]))

    def find_corrections(self, *, review_id: str, company_id: int) -> tuple[ReviewSourceInvoiceCorrection, ...]:
        return self._base.find_corrections(review_id=review_id, company_id=company_id)


@dataclass(slots=True)
class _Checks:
    items: list[CorrectionPreconditionCheck] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str) -> bool:
        status = CorrectionCheckStatus.PASSED if passed else CorrectionCheckStatus.FAILED
        self.items.append(CorrectionPreconditionCheck(name=name, status=status, detail=detail))
        return passed

    def skip(self, *names: str) -> None:
        for name in names:
            self.items.append(
                CorrectionPreconditionCheck(
                    name=name, status=CorrectionCheckStatus.SKIPPED, detail="not evaluated: an earlier check failed"
                )
            )

    @property
    def failed(self) -> bool:
        return any(check.status is CorrectionCheckStatus.FAILED for check in self.items)


CHECK_ORDER = (
    "review_exists",
    "not_already_applied",
    "review_pending",
    "version_matches",
    "no_decision",
    "no_write_authorization",
    "no_execution",
    "no_downstream_remediation",
    "source_document_present",
    "source_hash_matches",
    "reparse_succeeds",
    "source_evidence_present",
    "only_target_field_differs",
    "review_item_matches_source",
    "historical_identifier_matches_reason",
    "new_value_valid",
)


@dataclass(frozen=True, slots=True)
class _Verified:
    review: ReviewItem
    source: ReviewSourceInvoiceEvidence
    document: _SourceDocument
    old_value: str | None
    new_value: str


class CorrectReviewSourceIdentityUseCase:
    """Application boundary for one audited source-identity correction."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        source_reader: CorrectableSourceInvoiceReader,
        state_reader: SourceCorrectionStateReader,
        document_reader: SourceDocumentContentReader,
        resolver_factory: ResolverFactory,
        writer: SourceCorrectionWriter,
        unit_of_work: UnitOfWork,
        projection_synchronizer: ReviewProjectionSynchronizer | None = None,
        parse_document: Callable[[bytes], InternalInvoice] = parse_ubl_invoice,
        legacy_supplier_identifier: Callable[[bytes], str | None] = legacy_supplier_tax_identifier,
    ) -> None:
        self._review_reader = review_reader
        self._source_reader = source_reader
        self._state_reader = state_reader
        self._document_reader = document_reader
        self._resolver_factory = resolver_factory
        self._writer = writer
        self._unit_of_work = unit_of_work
        self._projection_synchronizer = projection_synchronizer
        self._parse_document = parse_document
        self._legacy_supplier_identifier = legacy_supplier_identifier

    async def execute(self, command: CorrectReviewSourceIdentityCommand) -> SourceIdentityCorrectionReport:
        if not isinstance(command, CorrectReviewSourceIdentityCommand):
            raise WorkbenchContractError("A canonical CorrectReviewSourceIdentityCommand is required.")
        checks = _Checks()
        review = self._load_review(command, checks)
        if review is None:
            return self._report(command, SourceIdentityCorrectionOutcome.REFUSED, checks)

        already = self._existing_correction(command)
        if already is not None:
            checks.record(
                "not_already_applied",
                False,
                f"correction v{already.from_version}->v{already.to_version} already recorded "
                f"({already.old_value!r} -> {already.new_value!r}); nothing written",
            )
            return self._report(
                command,
                SourceIdentityCorrectionOutcome.ALREADY_APPLIED,
                checks,
                old_value=already.old_value,
                new_value=already.new_value,
                from_version=already.from_version,
                to_version=already.to_version,
                safe_message="This correction was already applied; no business change was made.",
            )
        checks.record("not_already_applied", True, f"no correction recorded from v{command.expected_version}")

        verified_or_outcome = self._verify(command, review, checks)
        if isinstance(verified_or_outcome, SourceIdentityCorrectionReport):
            return verified_or_outcome
        verified = verified_or_outcome
        if verified is None:
            return self._report(command, SourceIdentityCorrectionOutcome.REFUSED, checks, from_version=review.version)

        correction = ReviewSourceInvoiceCorrection(
            review_id=command.review_id,
            company_id=command.company_id,
            from_version=command.expected_version,
            to_version=command.expected_version + 1,
            source_invoice_id=verified.source.source_invoice_id,
            field_path=command.field_path,
            old_value=verified.old_value,
            new_value=verified.new_value,
            source_document_id=verified.document.document_id,
            source_document_sha256=verified.document.content_sha256,
            reason=command.reason,
            approved_by=command.approved_by,
        )
        proposal = await self._proposal(command, correction)
        report = self._planned_report(command, checks, review, correction, proposal)
        if not command.apply:
            return report

        try:
            self._writer.apply_source_invoice_correction(correction, proposal)
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise
        logger.info(
            "review_source_identity_corrected",
            extra={
                "review_id": command.review_id,
                "company_id": command.company_id,
                "field_path": command.field_path.value,
                "reason": command.reason.value,
                "from_version": correction.from_version,
                "to_version": correction.to_version,
            },
        )
        return await self._after_commit(command, report)

    # ------------------------------------------------------------------ preconditions

    def _load_review(self, command: CorrectReviewSourceIdentityCommand, checks: _Checks) -> ReviewItem | None:
        try:
            review = self._review_reader.get_review_item(
                ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
            )
        except ReviewNotFoundError:
            checks.record("review_exists", False, "review not found for this company")
            checks.skip(*CHECK_ORDER[1:])
            return None
        checks.record("review_exists", True, f"status={review.status.value} version={review.version}")
        return review

    def _existing_correction(self, command: CorrectReviewSourceIdentityCommand) -> ReviewSourceInvoiceCorrection | None:
        for correction in self._source_reader.find_corrections(
            review_id=command.review_id, company_id=command.company_id
        ):
            if (
                correction.from_version == command.expected_version
                and correction.field_path is command.field_path
                and correction.reason is command.reason
            ):
                return correction
        return None

    def _verify(
        self, command: CorrectReviewSourceIdentityCommand, review: ReviewItem, checks: _Checks
    ) -> _Verified | SourceIdentityCorrectionReport | None:
        checks.record(
            "review_pending",
            review.status is ReviewStatus.PENDING_REVIEW,
            f"status={review.status.value}",
        )
        checks.record(
            "version_matches",
            review.version == command.expected_version,
            f"current v{review.version}, expected v{command.expected_version}",
        )
        self._check_downstream_state(command, checks)

        content, document = self._read_document(command, checks)
        if content is None or document is None:
            checks.skip(*CHECK_ORDER[CHECK_ORDER.index("reparse_succeeds") :])
            return None
        try:
            reparsed = self._parse_document(content)
        except Exception as exc:  # noqa: BLE001 - reported as a failed precondition, never swallowed silently
            checks.record("reparse_succeeds", False, f"current parser rejected the document: {type(exc).__name__}")
            checks.skip(*CHECK_ORDER[CHECK_ORDER.index("source_evidence_present") :])
            return None
        checks.record("reparse_succeeds", True, "parsed with the current UBL parser")

        try:
            source = self._source_reader.get(review_id=command.review_id, company_id=command.company_id)
        except ReviewNotFoundError:
            checks.record("source_evidence_present", False, "review has no immutable source-invoice evidence")
            checks.skip(*CHECK_ORDER[CHECK_ORDER.index("only_target_field_differs") :])
            return None
        checks.record("source_evidence_present", True, f"source invoice {source.source_invoice_id}")
        old_value = read_source_invoice_field(source.invoice, command.field_path)
        new_value = read_source_invoice_field(reparsed, command.field_path)
        differences = _differences(source.invoice, reparsed)
        if not differences:
            checks.record("only_target_field_differs", True, "effective source already equals the stored document")
            if checks.failed:
                return None
            return self._report(
                command,
                SourceIdentityCorrectionOutcome.NO_CHANGE,
                checks,
                old_value=old_value,
                new_value=new_value,
                from_version=review.version,
                safe_message="The effective source already matches the stored document; nothing to correct.",
            )
        checks.record(
            "only_target_field_differs",
            differences == (command.field_path.value,),
            f"differing paths: {', '.join(differences)}",
        )
        if command.field_path is SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER:
            checks.record(
                "review_item_matches_source",
                review.supplier_tax_number == old_value,
                f"review supplier_tax_number={review.supplier_tax_number!r}, effective source={old_value!r}",
            )
        self._check_historical_value(command, checks, content=content, old_value=old_value, new_value=new_value)
        checks.record(
            "new_value_valid",
            is_party_tax_identifier(new_value),
            f"{new_value!r} {'is' if is_party_tax_identifier(new_value) else 'is not'} a 10-digit VKN / 11-digit TCKN",
        )
        if checks.failed or new_value is None:
            return None
        return _Verified(review=review, source=source, document=document, old_value=old_value, new_value=new_value)

    def _check_downstream_state(self, command: CorrectReviewSourceIdentityCommand, checks: _Checks) -> None:
        counts = self._state_reader.downstream_state_counts(review_id=command.review_id)
        for group, check_name in (
            ("decision", "no_decision"),
            ("write_authorization", "no_write_authorization"),
            ("execution", "no_execution"),
            ("downstream_remediation", "no_downstream_remediation"),
        ):
            present = {table: count for table, count in counts.get(group, {}).items() if count}
            checks.record(
                check_name,
                not present,
                "none" if not present else ", ".join(f"{table}={count}" for table, count in sorted(present.items())),
            )

    def _read_document(
        self, command: CorrectReviewSourceIdentityCommand, checks: _Checks
    ) -> tuple[bytes | None, _SourceDocument | None]:
        documents = self._state_reader.find_source_documents(review_id=command.review_id, company_id=command.company_id)
        if len(documents) != 1:
            checks.record(
                "source_document_present", False, f"expected exactly 1 stored UBL document, found {len(documents)}"
            )
            checks.skip("source_hash_matches")
            return None, None
        document = documents[0]
        try:
            content = self._document_reader.read(document.storage_key)
        except Exception as exc:  # noqa: BLE001 - reported as a failed precondition
            checks.record(
                "source_document_present",
                False,
                f"document #{document.document_id} could not be read: {type(exc).__name__}",
            )
            checks.skip("source_hash_matches")
            return None, None
        checks.record("source_document_present", True, f"document #{document.document_id} ({len(content)} bytes)")
        digest = hashlib.sha256(content).hexdigest()
        if not checks.record(
            "source_hash_matches",
            digest == document.content_sha256,
            f"sha256 {digest[:16]}… vs stored {document.content_sha256[:16]}…",
        ):
            return None, None
        return content, document

    def _check_historical_value(
        self,
        command: CorrectReviewSourceIdentityCommand,
        checks: _Checks,
        *,
        content: bytes,
        old_value: str | None,
        new_value: str | None,
    ) -> None:
        if command.reason is SourceInvoiceCorrectionReason.UBL_PARTY_TAX_IDENTIFIER_PR201:
            # Replays the defective pre-PR #201 rule on the same stored document: the
            # persisted value must be exactly what that rule produced, and must differ
            # from what the current rule produces. No assumption about the shape of
            # the wrong identifier (MERSIS, plate, trade registry ...) is needed.
            try:
                legacy = self._legacy_supplier_identifier(content)
            except Exception as exc:  # noqa: BLE001 - reported as a failed precondition
                checks.record(
                    "historical_identifier_matches_reason",
                    False,
                    f"legacy rule could not be replayed: {type(exc).__name__}",
                )
                return
            checks.record(
                "historical_identifier_matches_reason",
                legacy is not None and legacy == old_value and legacy != new_value,
                f"pre-PR #201 rule yields {legacy!r}; persisted {old_value!r}; current rule {new_value!r}",
            )
            return
        checks.record("historical_identifier_matches_reason", False, "unsupported correction reason")

    # ------------------------------------------------------------------ classification

    async def _proposal(
        self, command: CorrectReviewSourceIdentityCommand, correction: ReviewSourceInvoiceCorrection
    ) -> ReviewReclassificationProposal:
        resolver = self._resolver_factory(_PendingCorrectionSourceReader(self._source_reader, correction))
        effective = await resolver.resolve(
            review_id=command.review_id,
            company_id=command.company_id,
            idempotency_key=f"source-correction:{command.company_id}:{command.review_id}:{command.expected_version}",
        )
        to_version = correction.to_version
        classification_evidence = build_review_classification_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            decision_result=effective.decision_result,
        )
        execution_evidence = build_review_execution_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            invoice=effective.source.invoice,
            decision_result=effective.execution_decision_result,
        )
        return ReviewReclassificationProposal(
            review_id=command.review_id,
            company_id=command.company_id,
            expected_version=command.expected_version,
            trigger=ReviewReclassificationTrigger.SOURCE_IDENTITY_CORRECTED,
            note=SOURCE_CORRECTION_NOTE.format(reason=command.reason.value, field=command.field_path.value),
            source_invoice_id=effective.source.source_invoice_id,
            new_workflow=effective.effective_workflow,
            new_review_reasons=effective.effective_review_reasons,
            new_warnings=effective.decision_result.warnings,
            matched_rule_code=classification_evidence.matched_rule_code if classification_evidence else None,
            matched_rule_id=classification_evidence.matched_rule_id if classification_evidence else None,
            new_classification_evidence=classification_evidence,
            new_execution_evidence=execution_evidence,
        )

    # ------------------------------------------------------------------ reporting

    def _planned_report(
        self,
        command: CorrectReviewSourceIdentityCommand,
        checks: _Checks,
        review: ReviewItem,
        correction: ReviewSourceInvoiceCorrection,
        proposal: ReviewReclassificationProposal,
    ) -> SourceIdentityCorrectionReport:
        previous_codes = tuple(reason.code.value for reason in review.review_reasons)
        new_codes = tuple(reason.code.value for reason in proposal.new_review_reasons)
        classification = proposal.new_classification_evidence
        hub_changes: list[ExpectedChange] = [
            ExpectedChange(
                "workbench_review_source_invoice_corrections",
                "INSERT",
                None,
                f"{correction.field_path.value}: {correction.old_value!r} -> {correction.new_value!r} "
                f"(v{correction.from_version}->v{correction.to_version}, reason {correction.reason.value}, "
                f"document #{correction.source_document_id})",
            ),
            ExpectedChange(_HUB_ITEMS, "supplier_tax_number", correction.old_value, correction.new_value),
            ExpectedChange(_HUB_ITEMS, "version", correction.from_version, correction.to_version),
        ]
        if review.workflow is not proposal.new_workflow:
            hub_changes.append(
                ExpectedChange(_HUB_ITEMS, "workflow", review.workflow.value, proposal.new_workflow.value)
            )
        if previous_codes != new_codes:
            hub_changes.append(ExpectedChange(_HUB_ITEMS, "review_reasons", previous_codes, new_codes))
        hub_changes.append(
            ExpectedChange(
                "workbench_review_classification_evidence",
                "INSERT",
                None,
                f"v{correction.to_version} status={classification.status.value if classification else None}",
            )
        )
        if proposal.new_execution_evidence is not None:
            hub_changes.append(
                ExpectedChange("workbench_review_execution_evidence", "INSERT", None, f"v{correction.to_version}")
            )
        hub_changes.append(
            ExpectedChange(
                "workbench_review_reclassifications",
                "INSERT",
                None,
                f"v{correction.from_version}->v{correction.to_version} trigger={proposal.trigger.value}",
            )
        )
        projection_changes: list[ExpectedChange] = [
            ExpectedChange("odoo_workbench", "supplier_tax_number", correction.old_value, correction.new_value),
            ExpectedChange("odoo_workbench", "review_version", correction.from_version, correction.to_version),
        ]
        if review.workflow is not proposal.new_workflow:
            projection_changes.append(
                ExpectedChange("odoo_workbench", "workflow", review.workflow.value, proposal.new_workflow.value)
            )
        if previous_codes != new_codes:
            projection_changes.append(ExpectedChange("odoo_workbench", "review_reasons", previous_codes, new_codes))
        projection_changes.append(ExpectedChange("odoo_workbench", "last_sync_at", "<previous>", "<sync time>"))
        return SourceIdentityCorrectionReport(
            review_id=command.review_id,
            company_id=command.company_id,
            outcome=SourceIdentityCorrectionOutcome.WOULD_APPLY,
            applied=False,
            field_path=command.field_path,
            reason=command.reason,
            checks=tuple(checks.items),
            old_value=correction.old_value,
            new_value=correction.new_value,
            from_version=correction.from_version,
            to_version=correction.to_version,
            previous_workflow=review.workflow.value,
            new_workflow=proposal.new_workflow.value,
            previous_reason_codes=previous_codes,
            new_reason_codes=new_codes,
            classification_status=classification.status.value if classification else None,
            hub_changes=tuple(hub_changes),
            projection_changes=tuple(projection_changes),
            safe_message="Dry-run: every precondition passed; --apply would write exactly these changes.",
        )

    async def _after_commit(
        self, command: CorrectReviewSourceIdentityCommand, report: SourceIdentityCorrectionReport
    ) -> SourceIdentityCorrectionReport:
        applied = replace(
            report,
            outcome=SourceIdentityCorrectionOutcome.APPLIED,
            applied=True,
            safe_message="Correction committed.",
        )
        try:
            # Off the event loop: the Odoo projection adapter is synchronous by design.
            sync = await asyncio.to_thread(
                sync_after_commit,
                self._projection_synchronizer,
                review_id=command.review_id,
                company_id=command.company_id,
            )
        except Exception as exc:  # noqa: BLE001 - the Hub correction is already durable
            logger.warning(
                "review_source_identity_projection_failed",
                extra={"review_id": command.review_id, "error_type": type(exc).__name__},
            )
            return replace(applied, projection_outcome="ERROR", projection_error=type(exc).__name__)
        if sync is None:
            return replace(applied, projection_outcome="DISABLED")
        return replace(
            applied,
            projection_outcome=sync.outcome.value,
            projection_odoo_record_id=sync.odoo_record_id,
            projection_error=sync.error,
        )

    def _report(
        self,
        command: CorrectReviewSourceIdentityCommand,
        outcome: SourceIdentityCorrectionOutcome,
        checks: _Checks,
        **values: object,
    ) -> SourceIdentityCorrectionReport:
        if outcome is SourceIdentityCorrectionOutcome.REFUSED and "safe_message" not in values:
            failed = sum(1 for check in checks.items if check.status is CorrectionCheckStatus.FAILED)
            values["safe_message"] = f"Refused: {failed} precondition(s) failed; nothing was written."
        return SourceIdentityCorrectionReport(
            review_id=command.review_id,
            company_id=command.company_id,
            outcome=outcome,
            applied=False,
            field_path=command.field_path,
            reason=command.reason,
            checks=tuple(checks.items),
            **values,
        )


def _differences(effective: InternalInvoice, reparsed: InternalInvoice) -> tuple[str, ...]:
    # InternalInvoice is a tree of frozen dataclasses: asdict() yields the same dotted
    # field paths as the persisted evidence payload (e.g. ``supplier.tax_number``).
    return diff_invoice_payloads(asdict(effective), asdict(reparsed))


__all__ = ["CHECK_ORDER", "CorrectReviewSourceIdentityUseCase"]
