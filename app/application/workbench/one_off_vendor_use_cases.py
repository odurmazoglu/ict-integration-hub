"""Crash-safe ONE_OFF_VENDOR archive-last orchestration (P0-PROD-08H).

See ``app.application.workbench.one_off_vendor_retirement`` for the state
machine and why archiving needs no analog to CREATE_NEW_PRODUCT's
NEEDS_RECONCILIATION-for-a-merely-uncertain-write problem: ``res.partner.active``
is a trivial, always-available read-back check, so a resume from
``ARCHIVE_ATTEMPTED`` always safely re-attempts (the underlying writer is itself
read-before-write idempotent) rather than dead-ending.
"""

from __future__ import annotations

import asyncio

from app.application.commands.one_off_vendor_retirement import ArchiveOneOffVendorPartnerCommand
from app.application.dto.one_off_vendor_retirement import OneOffVendorArchiveWriteStatus
from app.application.exceptions import ApplicationError
from app.application.exceptions.supplier_partner import (
    SupplierPartnerWriteAuthenticationError,
    SupplierPartnerWriteAuthorizationError,
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteValidationError,
)
from app.application.ports.one_off_vendor_retirement_writer import OneOffVendorRetirementPort
from app.application.services import UnitOfWork
from app.application.workbench.exceptions import OneOffVendorRetirementDataIntegrityError, OneOffVendorRetirementError
from app.application.workbench.one_off_vendor_retirement import (
    ArchiveOneOffVendorCommand,
    ArchiveOneOffVendorResult,
    ArchiveOneOffVendorStatus,
    OneOffVendorRetirementStatus,
)
from app.application.workbench.ports import OneOffVendorRetirementWriter, VendorBillExecutionEvidenceReader
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRepository,
    one_off_vendor_archive_authorization_consumer_id,
)

# Exceptions from OneOffVendorRetirementPort.archive_partner() that are CERTAIN to mean
# no Odoo write was attempted: the gate/policy check and payload validation run before
# any network call. Safe to revert ARCHIVE_ATTEMPTED back to PENDING_VENDOR_BILL. Every
# other exception is UNCERTAIN and leaves ARCHIVE_ATTEMPTED committed -- a resume's
# read-back-first behavior (inside the writer itself) is always safe to retry.
_CERTAIN_NO_WRITE_EXCEPTIONS = (
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteValidationError,
    SupplierPartnerWriteAuthenticationError,
    SupplierPartnerWriteAuthorizationError,
)


class ArchiveOneOffVendorUseCase:
    """Application boundary for one attempt to retire a review's ONE_OFF_VENDOR partner."""

    def __init__(
        self,
        *,
        retirement_writer: OneOffVendorRetirementWriter,
        vendor_bill_evidence_reader: VendorBillExecutionEvidenceReader,
        retirement_port: OneOffVendorRetirementPort,
        unit_of_work: UnitOfWork,
        approved_by: str | None = None,
        write_authorization_repository: WriteAuthorizationRepository | None = None,
        authorization_id: str | None = None,
    ) -> None:
        self._retirement_writer = retirement_writer
        self._vendor_bill_evidence_reader = vendor_bill_evidence_reader
        self._retirement_port = retirement_port
        self._unit_of_work = unit_of_work
        self._approved_by = approved_by
        # Optional (P0-PROD-09F): only the explicit operator recovery workflow ever
        # supplies these -- the automatic post-execution retirement trigger never
        # does, and remains gated by the existing global flag only.
        self._write_authorization_repository = write_authorization_repository
        self._authorization_id = authorization_id

    async def execute(self, command: ArchiveOneOffVendorCommand) -> ArchiveOneOffVendorResult:
        if not isinstance(command, ArchiveOneOffVendorCommand):
            raise OneOffVendorRetirementError("A canonical ArchiveOneOffVendorCommand is required.")

        retirement = self._retirement_writer.find(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.review_version,
        )
        if retirement is None:
            raise OneOffVendorRetirementDataIntegrityError(
                "No ONE_OFF_VENDOR retirement exists for this review version."
            )

        if retirement.status is OneOffVendorRetirementStatus.ARCHIVED:
            return self._result(retirement, status=ArchiveOneOffVendorStatus.ARCHIVED, already_applied=True)

        if retirement.status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL:
            has_evidence = self._vendor_bill_evidence_reader.has_successful_vendor_bill(
                review_id=command.review_id, company_id=command.company_id
            )
            if not has_evidence:
                return self._result(
                    retirement,
                    status=ArchiveOneOffVendorStatus.AWAITING_VENDOR_BILL,
                    already_applied=False,
                    safe_message="No durable Vendor Bill evidence yet; the partner is correctly not archived.",
                )
            # Commit the state-advance BEFORE the uncertain remote write -- mirrors
            # CreateNewProductUseCase's own crash-safety discipline exactly.
            retirement = self._retirement_writer.advance(
                retirement,
                expected_status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
                new_status=OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED,
            )
            self._unit_of_work.commit()

        # retirement.status is now ARCHIVE_ATTEMPTED or NEEDS_RECONCILIATION -- both
        # resume here. The writer is read-before-write idempotent, so this is always
        # safe to (re-)attempt regardless of how we got here.
        authorization = self._claim_write_authorization(command)
        try:
            write_result = await self._retirement_port.archive_partner(
                ArchiveOneOffVendorPartnerCommand(
                    partner_id=retirement.resolved_partner_id,
                    approved_by=self._approved_by,
                    authorization=authorization,
                )
            )
        except _CERTAIN_NO_WRITE_EXCEPTIONS:
            # P0-PROD-09F: discard the flushed-but-uncommitted authorization claim
            # (if any) before the retirement-state revert below commits -- a certain
            # no-write failure here always means one of the *unconditional* checks
            # (master kill switch, approval ack, named approver) failed despite a
            # valid authorization; none of those are fixed by retrying with the same
            # authorization, but the authorization itself must remain usable once
            # the real misconfiguration is fixed, exactly like an uncertain failure
            # below already preserves it via rollback.
            self._unit_of_work.rollback()
            if retirement.status is OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED:
                self._retirement_writer.advance(
                    retirement,
                    expected_status=OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED,
                    new_status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
                )
                self._unit_of_work.commit()
            raise
        except BaseException:
            # Uncertain remote outcome (transport failure, read-back/data-integrity
            # failure, or anything unexpected). Leave ARCHIVE_ATTEMPTED committed; a
            # resume's read-back-first behavior handles recovery -- never convert this
            # into a blind retry loop or a silent success.
            self._unit_of_work.rollback()
            if retirement.status is OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED:
                try:
                    self._retirement_writer.advance(
                        retirement,
                        expected_status=OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED,
                        new_status=OneOffVendorRetirementStatus.NEEDS_RECONCILIATION,
                    )
                    self._unit_of_work.commit()
                except ApplicationError:
                    self._unit_of_work.rollback()
            raise

        assert write_result.status in (
            OneOffVendorArchiveWriteStatus.ARCHIVED,
            OneOffVendorArchiveWriteStatus.ALREADY_ARCHIVED,
        )
        expected = (
            OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED
            if retirement.status is OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED
            else OneOffVendorRetirementStatus.NEEDS_RECONCILIATION
        )
        retirement = self._retirement_writer.advance(
            retirement,
            expected_status=expected,
            new_status=OneOffVendorRetirementStatus.ARCHIVED,
        )
        self._unit_of_work.commit()
        return self._result(retirement, status=ArchiveOneOffVendorStatus.ARCHIVED, already_applied=False)

    def _claim_write_authorization(self, command: ArchiveOneOffVendorCommand):
        """P0-PROD-09F: claim (and durably consume) the narrow write authorization for
        this exact archive attempt, if one was supplied. Deterministic consumer id
        from the command's own identity, so a legitimate crash-then-retry of the same
        recovery request resumes against its own already-consumed authorization."""

        if self._authorization_id is None:
            return None
        if self._write_authorization_repository is None:
            raise OneOffVendorRetirementError("Runtime authorization is not supported by this workflow.")
        return self._write_authorization_repository.claim_and_consume(
            company_id=command.company_id,
            review_id=command.review_id,
            operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE,
            target_version=command.review_version,
            authorization_id=self._authorization_id,
            execution_id=one_off_vendor_archive_authorization_consumer_id(
                company_id=command.company_id,
                review_id=command.review_id,
                review_version=command.review_version,
            ),
        )

    def _result(
        self,
        retirement,
        *,
        status: ArchiveOneOffVendorStatus,
        already_applied: bool,
        safe_message: str | None = None,
    ) -> ArchiveOneOffVendorResult:
        return ArchiveOneOffVendorResult(
            review_id=retirement.review_id,
            company_id=retirement.company_id,
            review_version=retirement.review_version,
            status=status,
            resolved_partner_id=retirement.resolved_partner_id,
            already_applied=already_applied,
            safe_message=safe_message
            or {
                ArchiveOneOffVendorStatus.ARCHIVED: "One-off vendor partner archived.",
                ArchiveOneOffVendorStatus.AWAITING_VENDOR_BILL: "Awaiting durable Vendor Bill evidence.",
                ArchiveOneOffVendorStatus.RECONCILIATION_REQUIRED: "Archive outcome requires manual reconciliation.",
            }[status],
        )


class OneOffVendorRetirementTrigger:
    """Best-effort post-Vendor-Bill retirement hook (P0-PROD-08I).

    The narrow production orchestration point for ``ArchiveOneOffVendorUseCase``: called
    once, synchronously, immediately after a durably-persisted, successful EXECUTE-mode
    accepted-decision run (never for DRY_RUN, never speculatively before that -- see
    ``RunAcceptedDecisionExecutionUseCase``). By the time this runs, any Vendor Bill step
    completed by that run is already durably committed, satisfying the archive-last
    invariant before this ever attempts anything.

    Most reviews carry no ONE_OFF_VENDOR retirement row at all -- ``find_latest_for_review``
    is a cheap no-op read for them. When a row exists, every outcome and every failure is
    delegated entirely to the existing, crash-safe use case; this class adds no
    state-machine logic of its own -- see ``one_off_vendor_retirement`` for why that use
    case is always safe to (re-)invoke regardless of readiness.

    Every failure is swallowed: a durably successful Vendor Bill must never be reported as
    failed merely because retirement could not complete (P0-PROD-08I s.7). The write gate
    being closed -- the default in every environment today -- is exactly such a failure;
    ``AWAITING_VENDOR_BILL``/``RECONCILIATION_REQUIRED`` are both expected, non-error
    outcomes of the underlying use case, never raised.
    """

    def __init__(
        self,
        *,
        retirement_writer: OneOffVendorRetirementWriter,
        archive_use_case: ArchiveOneOffVendorUseCase,
    ) -> None:
        self._retirement_writer = retirement_writer
        self._archive_use_case = archive_use_case

    def try_retire_after_execution(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> ArchiveOneOffVendorResult | None:
        try:
            retirement = self._retirement_writer.find_latest_for_review(review_id=review_id, company_id=company_id)
        except ApplicationError:
            return None
        if retirement is None:
            return None
        command = ArchiveOneOffVendorCommand(
            review_id=retirement.review_id,
            company_id=retirement.company_id,
            review_version=retirement.review_version,
        )
        try:
            return _run_archive(self._archive_use_case, command)
        except ApplicationError:
            return None


def _run_archive(
    use_case: ArchiveOneOffVendorUseCase,
    command: ArchiveOneOffVendorCommand,
) -> ArchiveOneOffVendorResult:
    # Mirrors vendor_bill_strategy._run_writer's exact bridge: this runs from the same sync
    # execution-runtime call stack that already used this idiom to perform the Vendor Bill
    # write itself, so no running loop is ever present here in practice.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(use_case.execute(command))
    raise OneOffVendorRetirementError("The retirement trigger cannot run inside an active event loop.")
