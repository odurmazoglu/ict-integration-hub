"""Crash-safe ONE_OFF_VENDOR archive-last orchestration (P0-PROD-08H).

See ``app.application.workbench.one_off_vendor_retirement`` for the state
machine and why archiving needs no analog to CREATE_NEW_PRODUCT's
NEEDS_RECONCILIATION-for-a-merely-uncertain-write problem: ``res.partner.active``
is a trivial, always-available read-back check, so a resume from
``ARCHIVE_ATTEMPTED`` always safely re-attempts (the underlying writer is itself
read-before-write idempotent) rather than dead-ending.
"""

from __future__ import annotations

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
    ) -> None:
        self._retirement_writer = retirement_writer
        self._vendor_bill_evidence_reader = vendor_bill_evidence_reader
        self._retirement_port = retirement_port
        self._unit_of_work = unit_of_work
        self._approved_by = approved_by

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
        try:
            write_result = await self._retirement_port.archive_partner(
                ArchiveOneOffVendorPartnerCommand(
                    partner_id=retirement.resolved_partner_id,
                    approved_by=self._approved_by,
                )
            )
        except _CERTAIN_NO_WRITE_EXCEPTIONS:
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
