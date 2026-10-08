"""Shared fakes for the effective-supplier abstraction (PR A)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.application.effective_supplier import AcceptedSupplierReader, EffectiveSupplierResolver
from app.erp.models import Partner
from app.matching import PartnerMatchResult, PartnerMatchStatus


class CanonicalPartners:
    """Fake read-only ``SupplierPartnerReader``.

    Every requested id is a canonical commercial head (its own commercial partner, no
    company) unless ``overrides`` says otherwise: a ``Partner`` to return, or ``None``
    for "not found in Odoo". ``calls`` records each requested id tuple.
    """

    def __init__(self, overrides: dict[int, Partner | None] | None = None, *, error: Exception | None = None) -> None:
        self.overrides = dict(overrides or {})
        self.error = error
        self.calls: list[tuple[int, ...]] = []

    def find_by_ids_including_archived(self, ids: Sequence[int]) -> Sequence[Partner]:
        self.calls.append(tuple(ids))
        if self.error is not None:
            raise self.error
        rows: list[Partner] = []
        for partner_id in ids:
            if partner_id in self.overrides:
                override = self.overrides[partner_id]
                if override is not None:
                    rows.append(override)
                continue
            rows.append(head(partner_id))
        return tuple(rows)


def head(partner_id: int, *, company_id: int | None = None, active: bool = True, vat: str | None = None) -> Partner:
    return Partner(
        id=partner_id,
        name=f"Partner {partner_id}",
        tax_number=vat,
        active=active,
        company_id=company_id,
        commercial_partner_id=partner_id,
    )


def contact(partner_id: int, *, parent_id: int, vat: str | None = None) -> Partner:
    return Partner(
        id=partner_id,
        name=f"Contact {partner_id}",
        tax_number=vat,
        active=True,
        parent_id=parent_id,
        commercial_partner_id=parent_id,
    )


class FixedPartnerMatcher:
    """Fake deterministic partner matcher returning one fixed raw result."""

    def __init__(self, result: PartnerMatchResult) -> None:
        self.result = result
        self.calls = 0

    def match_invoice(self, invoice: object, *, company_id: int | None = None) -> PartnerMatchResult:
        self.calls += 1
        return self.result


def raw_not_found() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.NOT_FOUND,
        partner_id=None,
        matched_by=None,
        reason="No active deterministic supplier partner candidate found.",
        candidate_count=0,
        confidence=None,
    )


def raw_ambiguous(candidate_count: int = 2) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MULTIPLE_MATCHES,
        partner_id=None,
        matched_by=None,
        reason="Multiple active supplier partner candidates found by tax number.",
        candidate_count=candidate_count,
        confidence=None,
    )


def raw_matched(partner_id: int) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=partner_id,
        matched_by="tax_number",
        reason="Unique supplier partner match by tax number.",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def effect_based_supplier_resolver(
    effect_reader: object,
    *,
    raw: PartnerMatchResult | None = None,
    partner_reader: CanonicalPartners | None = None,
) -> EffectiveSupplierResolver:
    """A real ``EffectiveSupplierResolver`` whose raw deterministic match is ``raw``
    (default NOT_FOUND, so the supplier can only come from the accepted resolution)."""

    return EffectiveSupplierResolver(
        partner_matcher=FixedPartnerMatcher(raw or raw_not_found()),
        accepted_supplier_reader=AcceptedSupplierReader(
            effect_reader=effect_reader,  # type: ignore[arg-type]
            partner_reader=partner_reader or CanonicalPartners(),
        ),
    )
