"""ONE_OFF_VENDOR archive-last lifecycle (P0-PROD-08H).

The *creation* side of ONE_OFF_VENDOR reuses existing, already-crash-safe
machinery unchanged: ``SupplierResolution`` (the reservation/intent, committed
before any Odoo write -- see ``ResolveWorkbenchSupplierUseCase._reserve_intent``)
and ``SupplierRemediationEffect`` (the append-only completed-effect record, now
also valid for ``mode=one_off_vendor``). Nothing new is needed there.

What's genuinely new is the *archive-last* half: retiring the Hub-owned partner
from ICT's active vendor population once -- and only once -- a Vendor Bill has
durably succeeded. This module is that state machine. One row per
``(review_id, company_id, review_version)``, created alongside the
``SupplierRemediationEffect`` in the same commit, advanced strictly forward:

    PENDING_VENDOR_BILL -> ARCHIVE_ATTEMPTED -> ARCHIVED
                                              -> (uncertain outcome; resume
                                                   read-back resolves it, see
                                                   OneOffVendorRetirementUseCase)

Unlike CREATE_NEW_PRODUCT's reservation (P0-PROD-07G), archiving has a trivial,
always-available idempotency check: ``res.partner.active`` is a plain boolean.
A resume from ``ARCHIVE_ATTEMPTED`` with an uncertain remote outcome can always
read the partner back and conclusively determine whether the archive already
happened -- 07G's product-master create has no equivalent durable identity
check available to it. ``NEEDS_RECONCILIATION`` therefore only covers a
genuine read failure (Odoo unreachable even for the read-back), never a merely
uncertain write.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError


class OneOffVendorRetirementStatus(StrEnum):
    """Durable state machine for one review's ONE_OFF_VENDOR archive lifecycle.

    Persisted immediately after the ``SupplierRemediationEffect`` is written (same
    commit) and advanced strictly forward. A resume always reads this status
    first.
    """

    #: The Hub-owned partner exists; no durable evidence of a successful Vendor
    #: Bill for this review yet. The partner may remain active -- this is
    #: expected and is explicitly NOT a completion state.
    PENDING_VENDOR_BILL = "pending_vendor_bill"
    #: The Odoo res.partner archive (active=False) write is about to be (or was)
    #: attempted; the remote outcome is UNKNOWN until ARCHIVED is persisted. A
    #: resume seeing this status must read the partner back (never blindly
    #: retry blind, never blindly report failure) -- see
    #: OneOffVendorRetirementUseCase.
    ARCHIVE_ATTEMPTED = "archive_attempted"
    #: res.partner.active=False durably confirmed (by the write's own read-back,
    #: or by a resume's read-back finding it already inactive). Terminal success.
    ARCHIVED = "archived"
    #: The archive outcome could not be determined even via read-back (e.g. Odoo
    #: unreachable on resume). Terminal; requires human reconciliation. Never
    #: reached merely because the original write's outcome was uncertain --
    #: only when read-back itself also fails.
    NEEDS_RECONCILIATION = "needs_reconciliation"


@dataclass(frozen=True, slots=True)
class OneOffVendorRetirement(ApplicationDTO):
    """The persisted archive-lifecycle row for one review's ONE_OFF_VENDOR resolution."""

    review_id: str
    company_id: int
    review_version: int
    resolved_partner_id: int
    status: OneOffVendorRetirementStatus

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_positive_int(self.resolved_partner_id, "resolved_partner_id must be positive.")
        if not isinstance(self.status, OneOffVendorRetirementStatus):
            raise WorkbenchContractError("A canonical OneOffVendorRetirementStatus is required.")


@dataclass(frozen=True, slots=True)
class ArchiveOneOffVendorCommand(ApplicationDTO):
    """An explicit request to attempt retirement of one review's ONE_OFF_VENDOR partner.

    Carries no partner identity or Vendor Bill evidence itself -- both are derived
    from persisted state (the retirement row and the durable workflow-execution
    record), never trusted from the caller.
    """

    review_id: str
    company_id: int
    review_version: int

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")


class ArchiveOneOffVendorStatus(StrEnum):
    """Outcome of one :class:`ArchiveOneOffVendorUseCase` request, returned to the caller."""

    #: res.partner.active=False durably confirmed.
    ARCHIVED = "archived"
    #: No durable Vendor Bill evidence yet -- correctly not archived. Not an error.
    AWAITING_VENDOR_BILL = "awaiting_vendor_bill"
    #: The archive outcome is unprovable even via read-back; needs human reconciliation.
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ArchiveOneOffVendorResult(ApplicationDTO):
    """Typed, stable result of :class:`ArchiveOneOffVendorUseCase`."""

    review_id: str
    company_id: int
    review_version: int
    status: ArchiveOneOffVendorStatus
    resolved_partner_id: int | None = None
    already_applied: bool = False
    safe_message: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        if not isinstance(self.status, ArchiveOneOffVendorStatus):
            raise WorkbenchContractError("A canonical ArchiveOneOffVendorStatus is required.")
        if self.resolved_partner_id is not None:
            _require_positive_int(self.resolved_partner_id, "resolved_partner_id must be positive when set.")


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise WorkbenchContractError(message)


__all__ = [
    "ArchiveOneOffVendorCommand",
    "ArchiveOneOffVendorResult",
    "ArchiveOneOffVendorStatus",
    "OneOffVendorRetirement",
    "OneOffVendorRetirementStatus",
]
