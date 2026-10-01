"""Append-only correction of a review's immutable source-invoice identity.

A review's ``ReviewSourceInvoiceEvidence`` is an insert-only snapshot of the
``InternalInvoice`` the Hub classified. When a since-fixed parser defect persisted a
wrong value into that snapshot (e.g. the pre-PR #201 UBL parser took a party's
MERSIS number as its tax number), the snapshot is **never rewritten**. Instead an
audited :class:`ReviewSourceInvoiceCorrection` is appended, and every consumer reads
the *effective* source invoice -- the original snapshot with all of its corrections
overlaid in ``to_version`` order -- through the normal source-evidence reader.

Each correction is bound to exactly one review version advance ``N -> N+1``. The
supported ``field_path`` / ``reason`` pairs are an explicit allow-list; nothing here
knows about specific invoices, suppliers or identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import ReviewDataIntegrityError, WorkbenchContractError
from app.domain.invoice import InternalInvoice
from app.domain.invoice.party_tax_identity import is_party_tax_identifier

SHA256_HEX_LENGTH = 64


class SourceInvoiceCorrectionField(StrEnum):
    """Source-invoice fields a correction may change. Deliberately an allow-list."""

    SUPPLIER_TAX_NUMBER = "supplier.tax_number"


class SourceInvoiceCorrectionReason(StrEnum):
    """Why a correction exists. Each reason is bound to the fields it may correct."""

    #: The pre-PR #201 UBL parser stored the first ``PartyIdentification/cbc:ID``
    #: (any scheme, e.g. MERSISNO) as the party tax number instead of the typed VKN/TCKN.
    UBL_PARTY_TAX_IDENTIFIER_PR201 = "UBL_PARTY_TAX_IDENTIFIER_PR201"


#: Which fields each reason may correct.
CORRECTABLE_FIELDS_BY_REASON: dict[SourceInvoiceCorrectionReason, frozenset[SourceInvoiceCorrectionField]] = {
    SourceInvoiceCorrectionReason.UBL_PARTY_TAX_IDENTIFIER_PR201: frozenset(
        {SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER}
    ),
}


@dataclass(frozen=True, slots=True)
class ReviewSourceInvoiceCorrection(ApplicationDTO):
    """One immutable, audited correction of a review's source-invoice snapshot."""

    review_id: str
    company_id: int
    from_version: int
    to_version: int
    source_invoice_id: str
    field_path: SourceInvoiceCorrectionField
    old_value: str | None
    new_value: str
    source_document_id: int
    source_document_sha256: str
    reason: SourceInvoiceCorrectionReason
    approved_by: str
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.from_version, "from_version must be positive.")
        if self.to_version != self.from_version + 1:
            raise WorkbenchContractError("A source correction advances the review by exactly one version.")
        _require_text(self.source_invoice_id, "source_invoice_id is required.")
        if not isinstance(self.field_path, SourceInvoiceCorrectionField):
            raise WorkbenchContractError("A supported source correction field_path is required.")
        if not isinstance(self.reason, SourceInvoiceCorrectionReason):
            raise WorkbenchContractError("A supported source correction reason is required.")
        if self.field_path not in CORRECTABLE_FIELDS_BY_REASON[self.reason]:
            raise WorkbenchContractError("The correction reason does not permit correcting this field.")
        _require_text(self.new_value, "new_value is required.")
        if self.old_value == self.new_value:
            raise WorkbenchContractError("A source correction must change the value.")
        if self.field_path is SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER and not is_party_tax_identifier(
            self.new_value
        ):
            raise WorkbenchContractError("A corrected supplier tax number must be a 10-digit VKN or 11-digit TCKN.")
        _require_positive_int(self.source_document_id, "source_document_id must be positive.")
        if (
            not isinstance(self.source_document_sha256, str)
            or len(self.source_document_sha256) != SHA256_HEX_LENGTH
            or any(char not in "0123456789abcdef" for char in self.source_document_sha256)
        ):
            raise WorkbenchContractError("source_document_sha256 must be a lowercase SHA-256 hex digest.")
        _require_text(self.approved_by, "approved_by (authenticated operator) is required.")


def read_source_invoice_field(invoice: InternalInvoice, field_path: SourceInvoiceCorrectionField) -> str | None:
    if field_path is SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER:
        return invoice.supplier.tax_number
    raise WorkbenchContractError("Unsupported source correction field_path.")


def write_source_invoice_field(
    invoice: InternalInvoice, field_path: SourceInvoiceCorrectionField, value: str
) -> InternalInvoice:
    if field_path is SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER:
        return replace(invoice, supplier=replace(invoice.supplier, tax_number=value))
    raise WorkbenchContractError("Unsupported source correction field_path.")


def apply_source_invoice_corrections(
    invoice: InternalInvoice,
    corrections: tuple[ReviewSourceInvoiceCorrection, ...] | list[ReviewSourceInvoiceCorrection],
) -> InternalInvoice:
    """Overlay ``corrections`` (in ``to_version`` order) on the immutable original invoice.

    Each correction must start from exactly the value the previous step produced; a
    broken chain is a data-integrity failure, never silently skipped.
    """

    effective = invoice
    for correction in sorted(corrections, key=lambda item: item.to_version):
        current = read_source_invoice_field(effective, correction.field_path)
        if current != correction.old_value:
            raise ReviewDataIntegrityError("Review source invoice correction chain is inconsistent.")
        effective = write_source_invoice_field(effective, correction.field_path, correction.new_value)
    return effective


def diff_invoice_payloads(left: Any, right: Any, path: str = "") -> tuple[str, ...]:
    """Dotted paths at which two serialized ``InternalInvoice`` payloads differ."""

    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(diff_invoice_payloads(left[key], right[key], child))
        return tuple(paths)
    if isinstance(left, list | tuple) and isinstance(right, list | tuple) and len(left) == len(right):
        paths = []
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            paths.extend(diff_invoice_payloads(left_item, right_item, f"{path}[{index}]"))
        return tuple(paths)
    return () if left == right else (path or "<root>",)


# --------------------------------------------------------------------------- command / report


@dataclass(frozen=True, slots=True)
class CorrectReviewSourceIdentityCommand(ApplicationDTO):
    """Operator request to correct one review's source identity (dry-run unless ``apply``)."""

    review_id: str
    company_id: int
    expected_version: int
    approved_by: str
    apply: bool = False
    field_path: SourceInvoiceCorrectionField = SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER
    reason: SourceInvoiceCorrectionReason = SourceInvoiceCorrectionReason.UBL_PARTY_TAX_IDENTIFIER_PR201

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        _require_text(self.approved_by, "approved_by (authenticated operator) is required.")
        if not isinstance(self.apply, bool):
            raise WorkbenchContractError("apply must be a boolean.")
        if not isinstance(self.field_path, SourceInvoiceCorrectionField):
            raise WorkbenchContractError("A supported source correction field_path is required.")
        if not isinstance(self.reason, SourceInvoiceCorrectionReason):
            raise WorkbenchContractError("A supported source correction reason is required.")
        if self.field_path not in CORRECTABLE_FIELDS_BY_REASON[self.reason]:
            raise WorkbenchContractError("The correction reason does not permit correcting this field.")


class CorrectionCheckStatus(StrEnum):
    PASSED = "PASS"
    FAILED = "FAIL"
    SKIPPED = "SKIP"


@dataclass(frozen=True, slots=True)
class CorrectionPreconditionCheck(ApplicationDTO):
    name: str
    status: CorrectionCheckStatus
    detail: str


class SourceIdentityCorrectionOutcome(StrEnum):
    #: Dry-run: every precondition passed; ``--apply`` would write exactly the listed changes.
    WOULD_APPLY = "WOULD_APPLY"
    #: The correction was committed (projection is best-effort afterwards).
    APPLIED = "APPLIED"
    #: A correction from this version already exists; nothing was written.
    ALREADY_APPLIED = "ALREADY_APPLIED"
    #: The effective source already equals the re-parsed document; nothing to correct.
    NO_CHANGE = "NO_CHANGE"
    #: At least one precondition failed; nothing was written.
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class ExpectedChange(ApplicationDTO):
    target: str
    field: str
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class SourceIdentityCorrectionReport(ApplicationDTO):
    review_id: str
    company_id: int
    outcome: SourceIdentityCorrectionOutcome
    applied: bool
    field_path: SourceInvoiceCorrectionField
    reason: SourceInvoiceCorrectionReason
    checks: tuple[CorrectionPreconditionCheck, ...] = field(default_factory=tuple)
    old_value: str | None = None
    new_value: str | None = None
    from_version: int | None = None
    to_version: int | None = None
    previous_workflow: str | None = None
    new_workflow: str | None = None
    previous_reason_codes: tuple[str, ...] = field(default_factory=tuple)
    new_reason_codes: tuple[str, ...] = field(default_factory=tuple)
    classification_status: str | None = None
    hub_changes: tuple[ExpectedChange, ...] = field(default_factory=tuple)
    projection_changes: tuple[ExpectedChange, ...] = field(default_factory=tuple)
    projection_outcome: str | None = None
    projection_odoo_record_id: int | None = None
    projection_error: str | None = None
    safe_message: str | None = None

    @property
    def failed_checks(self) -> tuple[CorrectionPreconditionCheck, ...]:
        return tuple(check for check in self.checks if check.status is CorrectionCheckStatus.FAILED)


def _require_text(value: object, message: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


__all__ = [
    "CORRECTABLE_FIELDS_BY_REASON",
    "CorrectReviewSourceIdentityCommand",
    "CorrectionCheckStatus",
    "CorrectionPreconditionCheck",
    "ExpectedChange",
    "ReviewSourceInvoiceCorrection",
    "SourceIdentityCorrectionOutcome",
    "SourceIdentityCorrectionReport",
    "SourceInvoiceCorrectionField",
    "SourceInvoiceCorrectionReason",
    "apply_source_invoice_corrections",
    "diff_invoice_payloads",
    "read_source_invoice_field",
    "write_source_invoice_field",
]
