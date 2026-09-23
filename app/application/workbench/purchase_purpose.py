"""Review-scoped purchase-purpose resolution (P0-PROD-15T).

Records *why* a purchase was made -- a business fact, immutable and pinned to
one exact review version, never an accounting decision by itself. See
``app.application.workbench.accounting_resolution`` for the separate *how it
should be posted* decision this purpose gates.

This is deliberately not an Odoo account and never touches the supplier-wide
``operating_expense_mappings`` table: a supplier can be mixed-purpose (e.g.
CloudSpark supplies both internal-use hardware and resale/customer-project
goods), so purpose is recorded per review, not per supplier.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError

PURCHASE_PURPOSE_NOTE_MAX_LENGTH = 1024


class PurchasePurpose(StrEnum):
    """Why a purchase was made. Never itself an accounting treatment."""

    INTERNAL_USE = "internal_use"
    RESALE = "resale"
    CUSTOMER_PROJECT = "customer_project"
    OTHER_OPERATING_EXPENSE = "other_operating_expense"


@dataclass(frozen=True, slots=True)
class SubmitPurchasePurposeCommand(ApplicationDTO):
    """An authenticated operator's explicit purchase-purpose statement for one review.

    ``approved_by`` is the authenticated actor supplied by the API security context,
    never a body field. ``company_id``/``source_invoice_id`` are never accepted from
    the caller either -- ``company_id`` comes from ``RequestContext``,
    ``source_invoice_id`` is derived from the review's own immutable source evidence.
    """

    review_id: str
    company_id: int
    expected_version: int
    purchase_purpose: PurchasePurpose
    approved_by: str
    note: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        if not isinstance(self.purchase_purpose, PurchasePurpose):
            raise WorkbenchContractError("A canonical PurchasePurpose is required.")
        _require_text(self.approved_by, "approved_by (authenticated actor) is required.")
        if self.note is not None:
            if not isinstance(self.note, str) or not self.note.strip():
                raise WorkbenchContractError("note must be non-empty text when provided.")
            if len(self.note) > PURCHASE_PURPOSE_NOTE_MAX_LENGTH:
                raise WorkbenchContractError(f"note must be {PURCHASE_PURPOSE_NOTE_MAX_LENGTH} characters or fewer.")


@dataclass(frozen=True, slots=True)
class PurchasePurposeResolution(ApplicationDTO):
    """The durable, immutable, review-scoped record of an accepted purchase purpose.

    Identity is ``(review_id, company_id, review_version)`` -- append-only, one per
    review version, mirrors ``SupplierRemediationEffect`` exactly.
    """

    review_id: str
    company_id: int
    review_version: int
    source_invoice_id: str
    purchase_purpose: PurchasePurpose
    approved_by: str | None = None
    note: str | None = None
    #: The persisted row's own id, populated by the repository once written/read;
    #: ``None`` only for an in-memory instance not yet persisted.
    id: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.source_invoice_id, "source_invoice_id is required.")
        if not isinstance(self.purchase_purpose, PurchasePurpose):
            raise WorkbenchContractError("A canonical PurchasePurpose is required.")
        if self.id is not None:
            _require_positive_int(self.id, "id must be positive when set.")


@dataclass(frozen=True, slots=True)
class PurchasePurposeSubmissionResult(ApplicationDTO):
    """Typed result of :class:`SubmitPurchasePurposeUseCase`."""

    review_id: str
    company_id: int
    review_version: int
    purchase_purpose: PurchasePurpose
    already_applied: bool
    safe_message: str | None = None


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


__all__ = [
    "PURCHASE_PURPOSE_NOTE_MAX_LENGTH",
    "PurchasePurpose",
    "PurchasePurposeResolution",
    "PurchasePurposeSubmissionResult",
    "SubmitPurchasePurposeCommand",
]
