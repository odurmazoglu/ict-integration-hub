"""PR F / F2: pure lifecycle rules for persisted advisory suggestion sets (ADR-0014).

No I/O and no clock: the caller passes ``now``. A set is generated for exactly one
Workbench product line version (review, version, line) from one Odoo catalog read; it
stays ``current`` while its inputs, fingerprints and engine version are unchanged, and is
displayable only while its last successful validation is at most ``DISPLAY_VALIDITY`` old.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.advisory.product_suggestions import ENGINE_VERSION, ProductSuggestionResult, SuggestionLine
from app.application.dto import ApplicationDTO

#: A suggestion set is never shown when its last successful catalog validation is older.
DISPLAY_VALIDITY = timedelta(minutes=20)
#: Superseded / invalidated history is kept this long; removal is a separate dry-run-first CLI.
HISTORY_RETENTION = timedelta(days=90)


class SuggestionSetStatus(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class SuggestionSetReason(StrEnum):
    REVIEW_VERSION_CHANGED = "review_version_changed"
    LINE_RESOLVED = "line_resolved"
    REVIEW_NOT_PENDING = "review_not_pending"
    INPUT_CHANGED = "input_changed"
    CATALOG_CHANGED = "catalog_changed"
    SUPPLIERINFO_CHANGED = "supplierinfo_changed"
    ENGINE_CHANGED = "engine_changed"


@dataclass(frozen=True, slots=True)
class AdvisoryLine(ApplicationDTO):
    """One current PRODUCT_NOT_FOUND Workbench line, from committed Hub evidence only.

    ``supplier_partner_id`` is the effective commercial supplier the persisted product
    matching of ``review_version`` ran under -- never a raw invoice supplier id.
    """

    company_id: int
    review_id: str
    review_version: int
    line_number: str
    line_key: str
    line_sequence: int
    supplier_partner_id: int
    seller_item_code: str | None
    description: str | None
    manufacturer_item_code: str | None = None

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.review_id, self.review_version, self.line_number)

    def suggestion_line(self) -> SuggestionLine:
        return SuggestionLine(
            review_id=self.review_id,
            company_id=self.company_id,
            review_version=self.review_version,
            line_number=self.line_number,
            supplier_partner_id=self.supplier_partner_id,
            seller_item_code=self.seller_item_code,
            description=self.description,
            manufacturer_item_code=self.manufacturer_item_code,
        )

    @property
    def input_digest(self) -> str:
        payload = [self.supplier_partner_id, self.seller_item_code, self.manufacturer_item_code, self.description]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredSuggestionSet(ApplicationDTO):
    id: int
    company_id: int
    review_id: str
    review_version: int
    line_number: str
    line_key: str
    supplier_partner_id: int | None
    input_digest: str
    catalog_fingerprint: str
    supplierinfo_fingerprint: str
    engine_version: str
    status: SuggestionSetStatus
    status_reason: SuggestionSetReason | None
    generated_at: datetime
    validated_at: datetime
    result: ProductSuggestionResult

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.review_id, self.review_version, self.line_number)


def replacement_reason(
    stored: StoredSuggestionSet,
    line: AdvisoryLine,
    *,
    catalog_fingerprint: str,
    supplierinfo_fingerprint: str,
    engine_version: str = ENGINE_VERSION,
) -> SuggestionSetReason | None:
    """Why a current set of the same line version must be regenerated (``None``: still valid)."""

    if stored.engine_version != engine_version:
        return SuggestionSetReason.ENGINE_CHANGED
    if stored.input_digest != line.input_digest:
        return SuggestionSetReason.INPUT_CHANGED
    if stored.catalog_fingerprint != catalog_fingerprint:
        return SuggestionSetReason.CATALOG_CHANGED
    if stored.supplierinfo_fingerprint != supplierinfo_fingerprint:
        return SuggestionSetReason.SUPPLIERINFO_CHANGED
    return None


def retirement(
    stored: StoredSuggestionSet, *, pending_versions: dict[str, int]
) -> tuple[SuggestionSetStatus, SuggestionSetReason]:
    """Status for a current set whose line version is no longer a current PRODUCT_NOT_FOUND line."""

    version = pending_versions.get(stored.review_id)
    if version is None:
        return SuggestionSetStatus.INVALIDATED, SuggestionSetReason.REVIEW_NOT_PENDING
    if version != stored.review_version:
        return SuggestionSetStatus.SUPERSEDED, SuggestionSetReason.REVIEW_VERSION_CHANGED
    return SuggestionSetStatus.INVALIDATED, SuggestionSetReason.LINE_RESOLVED


def is_displayable(stored: StoredSuggestionSet, *, now: datetime) -> bool:
    """Only a current set validated by a successful catalog read within ``DISPLAY_VALIDITY``."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    return stored.status is SuggestionSetStatus.CURRENT and now - stored.validated_at <= DISPLAY_VALIDITY


__all__ = [
    "DISPLAY_VALIDITY",
    "HISTORY_RETENTION",
    "AdvisoryLine",
    "StoredSuggestionSet",
    "SuggestionSetReason",
    "SuggestionSetStatus",
    "is_displayable",
    "replacement_reason",
    "retirement",
]
