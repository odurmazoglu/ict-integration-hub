"""The effective supplier of one review -- the single authority for "which commercial
supplier does this review's product resolution and execution use right now".

Before this module the answer was split: :class:`~app.application.use_cases.
effective_decision.EffectiveDecisionResolver` substituted an accepted
``SupplierRemediationEffect`` partner into *execution evidence* only, while product
matching (``DeterministicRuleEngine`` -> ``ProductMatchingEngine``) always received
the *raw* deterministic partner match. A review whose raw match was ambiguous but
whose operator accepted ``MATCH_EXISTING -> partner X`` therefore executed against X
and had supplierinfo written for X, yet reclassification kept looking supplierinfo
up under "no supplier" -- so the mapped line stayed PRODUCT_NOT_FOUND forever.

Every consumer now uses the same rule (:func:`resolve_effective_supplier`):

1. a raw deterministic match (exact VAT, collapsed to its commercial partner by the
   #206 ``PartnerMatchingEngine``) is authoritative and wins -- unchanged precedence;
2. otherwise an *accepted* review-scoped supplier resolution (MATCH_EXISTING,
   CREATE_PERMANENT_SUPPLIER or ONE_OFF_VENDOR effect) whose partner is proven, by a
   read-only Odoo read, to be its own commercial partner in a compatible company;
3. otherwise there is no effective supplier (fail closed).

Never fuzzy, never name-based, never a write. Kept free of ``supplier_resolution`` /
``supplier_remediation`` imports (structural types only), like ``effective_decision``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.domain.invoice import InternalInvoice
from app.erp.models import Partner
from app.matching import PartnerMatchResult, PartnerMatchStatus
from app.matching.exceptions import MatchingError
from app.matching.partner import canonical_partner_id

logger = logging.getLogger(__name__)

#: ``matched_by`` / confidence of the partner match synthesized from an accepted
#: supplier resolution (unchanged from P0-PROD-10D, re-exported by ``effective_decision``).
ACCEPTED_SUPPLIER_MATCHED_BY = "supplier_remediation_effect"
ACCEPTED_SUPPLIER_MATCH_CONFIDENCE = Decimal("1.00")

SAFE_EFFECTIVE_SUPPLIER_READ_ERROR = "Tedarikçi bilgisi Odoo'dan şu an okunamadı; birazdan tekrar deneyin."


def _read_error() -> Exception:
    # Imported here, not at module level: ``app.application.workbench`` imports the
    # product-remediation use cases, which import this module (import cycle).
    from app.application.workbench.exceptions import EffectiveSupplierReadError

    return EffectiveSupplierReadError(SAFE_EFFECTIVE_SUPPLIER_READ_ERROR)


class EffectiveSupplierOrigin(StrEnum):
    """Where the effective supplier came from. Values of the accepted origins are the
    exact ``SupplierResolutionMode`` values (compared as plain strings)."""

    DETERMINISTIC = "deterministic"
    MATCH_EXISTING = "match_existing"
    CREATE_PERMANENT_SUPPLIER = "create_permanent_supplier"
    ONE_OFF_VENDOR = "one_off_vendor"


_ACCEPTED_ORIGINS = frozenset(
    {
        EffectiveSupplierOrigin.MATCH_EXISTING,
        EffectiveSupplierOrigin.CREATE_PERMANENT_SUPPLIER,
        EffectiveSupplierOrigin.ONE_OFF_VENDOR,
    }
)


class AcceptedRemediationEffect(Protocol):
    """Structural ``SupplierRemediationEffect`` (only the fields read here)."""

    mode: str
    resolved_partner_id: int


class RemediationEffectReader(Protocol):
    def find_latest_remediation_effect(
        self, *, review_id: str, company_id: int
    ) -> AcceptedRemediationEffect | None: ...


class SupplierPartnerReader(Protocol):
    """Read-only ``res.partner`` by id, archived rows included (an archived Hub-owned
    ONE_OFF_VENDOR partner is a legitimate accepted supplier -- P0-PROD-10D)."""

    def find_by_ids_including_archived(self, ids: Sequence[int]) -> Sequence[Partner]: ...


class DeterministicPartnerMatcher(Protocol):
    def match_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> PartnerMatchResult: ...


@dataclass(frozen=True, slots=True)
class AcceptedSupplier:
    """An accepted review-scoped supplier resolution whose partner has been proven canonical."""

    origin: EffectiveSupplierOrigin
    partner_id: int

    def __post_init__(self) -> None:
        if self.origin not in _ACCEPTED_ORIGINS:
            raise ValueError("An accepted supplier needs an accepted-resolution origin.")
        if type(self.partner_id) is not int or self.partner_id <= 0:
            raise ValueError("An accepted supplier needs a positive partner id.")


class AcceptedSupplierStatus(StrEnum):
    #: No accepted supplier resolution exists for the review.
    NONE = "none"
    PROVEN = "proven"
    #: A resolution exists but its partner cannot be proven canonical -- never used.
    UNPROVEN = "unproven"


@dataclass(frozen=True, slots=True)
class AcceptedSupplierLookup:
    status: AcceptedSupplierStatus
    supplier: AcceptedSupplier | None = None
    #: Operator-safe Turkish explanation for UNPROVEN.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveSupplier:
    """Exactly one canonical commercial supplier, plus the partner match every matcher
    and the execution evidence consume (the raw match itself for DETERMINISTIC)."""

    partner_id: int
    origin: EffectiveSupplierOrigin
    partner_match: PartnerMatchResult

    @property
    def accepted(self) -> bool:
        return self.origin is not EffectiveSupplierOrigin.DETERMINISTIC


@dataclass(frozen=True, slots=True)
class EffectiveSupplierResolution:
    """Outcome for a use case: ``supplier`` or an operator-safe ``failure`` (never both)."""

    supplier: EffectiveSupplier | None
    failure: str | None = None


class EffectiveSupplierResolverPort(Protocol):
    """What product-remediation use cases depend on (satisfied by :class:`EffectiveSupplierResolver`)."""

    def resolve(self, *, review_id: str, company_id: int, invoice: InternalInvoice) -> EffectiveSupplierResolution: ...


UNRESOLVED_SUPPLIER_MESSAGE = "Bu incelemede tedarikçi henüz kesin olarak eşleşmedi; önce tedarikçi adımı tamamlanmalı."


def resolve_effective_supplier(
    raw_partner_match: PartnerMatchResult | None,
    accepted: AcceptedSupplier | None,
) -> EffectiveSupplier | None:
    """The one precedence rule. Pure; the inputs are already proven."""

    if _is_matched(raw_partner_match):
        assert raw_partner_match is not None and raw_partner_match.partner_id is not None
        return EffectiveSupplier(
            partner_id=raw_partner_match.partner_id,
            origin=EffectiveSupplierOrigin.DETERMINISTIC,
            partner_match=raw_partner_match,
        )
    if accepted is not None:
        return EffectiveSupplier(
            partner_id=accepted.partner_id,
            origin=accepted.origin,
            partner_match=accepted_supplier_partner_match(accepted.partner_id),
        )
    return None


def supplier_match_for_product_matching(
    raw_partner_match: PartnerMatchResult | None,
    accepted: AcceptedSupplier | None,
) -> PartnerMatchResult | None:
    """The partner match product matching must use: the effective supplier's, else the raw one."""

    effective = resolve_effective_supplier(raw_partner_match, accepted)
    return effective.partner_match if effective is not None else raw_partner_match


def accepted_supplier_partner_match(partner_id: int) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=partner_id,
        matched_by=ACCEPTED_SUPPLIER_MATCHED_BY,
        reason="Resolved via an accepted supplier remediation effect for this review.",
        candidate_count=1,
        confidence=ACCEPTED_SUPPLIER_MATCH_CONFIDENCE,
    )


class AcceptedSupplierReader:
    """Loads the review's accepted supplier resolution and proves its partner (read-only).

    Proof reuses the #206 canonicalization rule (``canonical_partner_id``): the partner
    must exist and be its *own* commercial partner. A contact is never silently rewritten
    to its parent -- exactly like MATCH_EXISTING validation, the accepted intent must name
    the partner that becomes effective, so a contact fails closed.
    """

    def __init__(self, *, effect_reader: RemediationEffectReader, partner_reader: SupplierPartnerReader) -> None:
        self._effect_reader = effect_reader
        self._partner_reader = partner_reader

    def lookup(self, *, review_id: str, company_id: int) -> AcceptedSupplierLookup:
        effect = self._effect_reader.find_latest_remediation_effect(review_id=review_id, company_id=company_id)
        if effect is None:
            return AcceptedSupplierLookup(status=AcceptedSupplierStatus.NONE)
        origin = _accepted_origin(effect.mode)
        partner_id = effect.resolved_partner_id
        if origin is None or type(partner_id) is not int or partner_id <= 0:
            return self._unproven(review_id, partner_id, "Kabul edilen tedarikçi kararı desteklenmiyor.")
        try:
            rows = self._partner_reader.find_by_ids_including_archived((partner_id,))
        except Exception as exc:  # noqa: BLE001 - surfaced as a safe, transient error; never treated as "no supplier"
            raise _read_error() from exc
        partners = [row for row in rows if row.id == partner_id]
        if len(partners) != 1:
            return self._unproven(review_id, partner_id, "Kabul edilen tedarikçi Odoo'da bulunamadı.")
        partner = partners[0]
        if canonical_partner_id(partner) != partner.id:
            return self._unproven(
                review_id,
                partner_id,
                f"Kabul edilen tedarikçi bir ticari firmanın ({canonical_partner_id(partner)}) alt kişisi; "
                "tedarikçi adımı ticari firma ile yeniden yapılmalı.",
            )
        if partner.company_id not in (None, company_id):
            return self._unproven(review_id, partner_id, "Kabul edilen tedarikçi başka bir şirkete ait.")
        return AcceptedSupplierLookup(
            status=AcceptedSupplierStatus.PROVEN,
            supplier=AcceptedSupplier(origin=origin, partner_id=partner_id),
        )

    @staticmethod
    def _unproven(review_id: str, partner_id: object, reason: str) -> AcceptedSupplierLookup:
        logger.warning(
            "effective_supplier.accepted_supplier_unproven",
            extra={"review_id": review_id, "partner_id": partner_id, "reason": reason},
        )
        return AcceptedSupplierLookup(status=AcceptedSupplierStatus.UNPROVEN, reason=reason)


class EffectiveSupplierResolver:
    """Effective supplier of a review *now*, for product-remediation use cases.

    Runs the same deterministic partner matcher reclassification runs, then the same
    :func:`resolve_effective_supplier` rule, so the supplier a use case writes
    supplierinfo for is exactly the supplier its follow-up reclassification matches
    products under.
    """

    def __init__(
        self,
        *,
        partner_matcher: DeterministicPartnerMatcher,
        accepted_supplier_reader: AcceptedSupplierReader,
    ) -> None:
        self._partner_matcher = partner_matcher
        self._accepted_supplier_reader = accepted_supplier_reader

    def resolve(self, *, review_id: str, company_id: int, invoice: InternalInvoice) -> EffectiveSupplierResolution:
        try:
            raw = self._partner_matcher.match_invoice(invoice, company_id=company_id)
        except MatchingError as exc:
            raise _read_error() from exc
        effective = resolve_effective_supplier(raw, None)
        if effective is not None:
            return EffectiveSupplierResolution(supplier=effective)
        lookup = self._accepted_supplier_reader.lookup(review_id=review_id, company_id=company_id)
        if lookup.status is AcceptedSupplierStatus.UNPROVEN:
            return EffectiveSupplierResolution(supplier=None, failure=lookup.reason or UNRESOLVED_SUPPLIER_MESSAGE)
        effective = resolve_effective_supplier(raw, lookup.supplier)
        if effective is None:
            return EffectiveSupplierResolution(supplier=None, failure=UNRESOLVED_SUPPLIER_MESSAGE)
        return EffectiveSupplierResolution(supplier=effective)


def _is_matched(match: PartnerMatchResult | None) -> bool:
    return (
        isinstance(match, PartnerMatchResult)
        and match.status is PartnerMatchStatus.MATCHED
        and type(match.partner_id) is int
        and match.partner_id > 0
    )


def _accepted_origin(mode: object) -> EffectiveSupplierOrigin | None:
    try:
        origin = EffectiveSupplierOrigin(str(mode))
    except ValueError:
        return None
    return origin if origin in _ACCEPTED_ORIGINS else None


__all__ = [
    "ACCEPTED_SUPPLIER_MATCHED_BY",
    "ACCEPTED_SUPPLIER_MATCH_CONFIDENCE",
    "SAFE_EFFECTIVE_SUPPLIER_READ_ERROR",
    "UNRESOLVED_SUPPLIER_MESSAGE",
    "AcceptedRemediationEffect",
    "AcceptedSupplier",
    "AcceptedSupplierLookup",
    "AcceptedSupplierReader",
    "AcceptedSupplierStatus",
    "EffectiveSupplier",
    "EffectiveSupplierOrigin",
    "EffectiveSupplierResolution",
    "EffectiveSupplierResolver",
    "EffectiveSupplierResolverPort",
    "RemediationEffectReader",
    "SupplierPartnerReader",
    "accepted_supplier_partner_match",
    "resolve_effective_supplier",
    "supplier_match_for_product_matching",
]
