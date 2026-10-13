"""PR F / F2 + F3.1: pure lifecycle and display rules for persisted advisory suggestion sets (ADR-0014).

No I/O and no clock: the caller passes ``now``. A set is generated for exactly one
Workbench product line version (review, version, line) from one Odoo catalog read; it
stays ``current`` while its inputs, fingerprints and engine version are unchanged, and is
displayable only while its last successful validation is at most ``DISPLAY_VALIDITY`` old.

F3.1 display contract: the Workbench projection shows a set only while it is at most
``PROJECTION_CUTOFF`` (validity minus one sweep interval) old, so the 60 s display sweep
can clear it from Odoo before ``DISPLAY_VALIDITY`` ends. ``display_synced_at`` is the start
of the last successful Odoo refresh that reflected the set's display state; it never
extends validity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app.advisory.product_suggestions import ENGINE_VERSION, ProductSuggestionResult, SuggestionLine
from app.application.dto import ApplicationDTO

#: A suggestion set is never shown when its last successful catalog validation is older.
DISPLAY_VALIDITY = timedelta(minutes=20)
#: Superseded / invalidated history is kept this long; removal is a separate dry-run-first CLI.
HISTORY_RETENTION = timedelta(days=90)
#: F3.1: the database-only display sweep runs this often on the Hub worker.
DISPLAY_SWEEP_INTERVAL = timedelta(seconds=60)
#: F3.1: the projection never shows a set older than this (one sweep interval of margin).
PROJECTION_CUTOFF = DISPLAY_VALIDITY - DISPLAY_SWEEP_INTERVAL


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


class DisplayBlock(StrEnum):
    """Why a persisted set is not shown on a projected Workbench line (F3.1)."""

    REVIEW_NOT_PENDING = "review_not_pending"
    VERSION_MISMATCH = "version_mismatch"
    LINE_NOT_PRODUCT_NOT_FOUND = "line_not_product_not_found"
    SET_NOT_CURRENT = "set_not_current"
    LINE_IDENTITY_MISMATCH = "line_identity_mismatch"
    SUPPLIER_MISMATCH = "supplier_mismatch"
    SELLER_CODE_MISMATCH = "seller_code_mismatch"
    VALIDATION_EXPIRED = "validation_expired"
    NO_CANDIDATES = "no_candidates"


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
    #: F3.1: the seller code the set was generated for (display gate).
    seller_item_code: str | None = None
    #: F3.1: start of the last successful Odoo refresh reflecting this set's display state.
    display_synced_at: datetime | None = None
    #: F3.1: ``(product id, name snapshot)`` of the candidates, as read from the catalog.
    product_names: tuple[tuple[int, str], ...] = field(default_factory=tuple)

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.review_id, self.review_version, self.line_number)

    def product_name(self, product_id: int) -> str | None:
        return dict(self.product_names).get(product_id)


@dataclass(frozen=True, slots=True)
class DisplayedLine:
    """The projected Workbench line a set would be shown on (committed Hub facts only)."""

    review_pending: bool
    review_version: int
    line_key: str
    line_number: str
    product_not_found: bool
    supplier_partner_id: int | None
    seller_item_code: str | None


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

    _require_aware(now)
    return stored.status is SuggestionSetStatus.CURRENT and now - stored.validated_at <= DISPLAY_VALIDITY


def is_projectable(stored: StoredSuggestionSet, *, now: datetime) -> bool:
    """F3.1: within the conservative projection cutoff (19 of the 20 minutes)."""

    _require_aware(now)
    return now - stored.validated_at <= PROJECTION_CUTOFF


def display_block(stored: StoredSuggestionSet, line: DisplayedLine, *, now: datetime) -> DisplayBlock | None:
    """F3.1 display gate: ``None`` only when every condition holds for exactly this line."""

    _require_aware(now)
    if not line.review_pending:
        return DisplayBlock.REVIEW_NOT_PENDING
    if stored.review_version != line.review_version:
        return DisplayBlock.VERSION_MISMATCH
    if not line.product_not_found:
        return DisplayBlock.LINE_NOT_PRODUCT_NOT_FOUND
    if stored.status is not SuggestionSetStatus.CURRENT:
        return DisplayBlock.SET_NOT_CURRENT
    if stored.line_key != line.line_key or stored.line_number != line.line_number:
        return DisplayBlock.LINE_IDENTITY_MISMATCH
    if line.supplier_partner_id is None or stored.supplier_partner_id != line.supplier_partner_id:
        return DisplayBlock.SUPPLIER_MISMATCH
    if stored.seller_item_code != line.seller_item_code:
        return DisplayBlock.SELLER_CODE_MISMATCH
    if not is_projectable(stored, now=now):
        return DisplayBlock.VALIDATION_EXPIRED
    if not stored.result.suggestions:
        return DisplayBlock.NO_CANDIDATES
    return None


def display_cleared(validated_at: datetime, display_synced_at: datetime | None) -> bool:
    """The last successful refresh started after the set left the projection cutoff.

    Such a set is not shown in Odoo, and no later projection (of any flow) can show it
    again until a successful revalidation moves ``validated_at``.
    """

    return display_synced_at is not None and display_synced_at >= validated_at + PROJECTION_CUTOFF


def needs_display_refresh(stored: StoredSuggestionSet, *, now: datetime) -> bool:
    """F3.1 sweep rule (database facts only): Odoo may show something other than the Hub gate allows.

    * never refreshed since its display state last changed (new, retired, revalidated after
      being cleared) -> refresh;
    * current, past the projection cutoff, and not refreshed since -> refresh (clear);
    * otherwise nothing to do -- in particular an already-cleared set is never refreshed again.
    """

    _require_aware(now)
    if stored.display_synced_at is None:
        return True
    if stored.status is not SuggestionSetStatus.CURRENT:
        return False
    return not is_projectable(stored, now=now) and not display_cleared(stored.validated_at, stored.display_synced_at)


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")


__all__ = [
    "DISPLAY_SWEEP_INTERVAL",
    "DISPLAY_VALIDITY",
    "HISTORY_RETENTION",
    "PROJECTION_CUTOFF",
    "AdvisoryLine",
    "DisplayBlock",
    "DisplayedLine",
    "StoredSuggestionSet",
    "SuggestionSetReason",
    "SuggestionSetStatus",
    "display_block",
    "display_cleared",
    "is_displayable",
    "is_projectable",
    "needs_display_refresh",
    "replacement_reason",
    "retirement",
]
