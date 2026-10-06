from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.invoice import InternalInvoice
from app.erp.models import Partner
from app.erp.provider import RepositoryProvider
from app.erp.repositories.partner import PartnerRepository
from app.matching.exceptions import PartnerMatchingError
from app.matching.result import PartnerMatchResult, PartnerMatchStatus

EXACT_MATCH_CONFIDENCE = Decimal("1.00")


class PartnerMatchingEngine:
    """Deterministic supplier matcher for imported invoices."""

    def __init__(self, provider: RepositoryProvider) -> None:
        self._provider = provider

    def match_invoice(self, invoice: object, *, company_id: int | None = None) -> PartnerMatchResult:
        if not isinstance(invoice, InternalInvoice):
            return _result(
                status=PartnerMatchStatus.INVALID_INPUT,
                partner_id=None,
                matched_by=None,
                reason="InternalInvoice DTO is required for supplier matching.",
                candidate_count=0,
                confidence=None,
            )

        tax_number = _clean(invoice.supplier.tax_number)
        if tax_number is None:
            return _result(
                status=PartnerMatchStatus.INVALID_INPUT,
                partner_id=None,
                matched_by=None,
                reason="Supplier tax number is required for deterministic matching.",
                candidate_count=0,
                confidence=None,
            )

        try:
            candidates = self._provider.partner_repository.find_by_tax_number(tax_number, company_id=company_id)
        except Exception as exc:
            raise PartnerMatchingError("Partner repository lookup failed.") from exc

        active_candidates = _active_candidates(candidates)
        if not active_candidates:
            return _result(
                status=PartnerMatchStatus.NOT_FOUND,
                partner_id=None,
                matched_by=None,
                reason="No active deterministic supplier partner candidate found.",
                candidate_count=0,
                confidence=None,
            )

        # A company and its child contacts (same commercial_partner_id) are ONE
        # commercial counterparty: ambiguity is decided on canonical commercial
        # partners, never on raw res.partner row count.
        try:
            groups = group_by_commercial_partner(active_candidates, self._provider.partner_repository)
        except Exception as exc:
            raise PartnerMatchingError("Partner repository lookup failed.") from exc

        if len(groups) > 1:
            return _result(
                status=PartnerMatchStatus.MULTIPLE_MATCHES,
                partner_id=None,
                matched_by=None,
                reason="Multiple active supplier partner candidates found by tax number.",
                candidate_count=len(groups),
                confidence=None,
            )

        group = groups[0]
        if not _is_valid_canonical_partner(group, tax_number=tax_number, company_id=company_id):
            # Fail closed: the rows share one commercial partner, but that partner itself
            # cannot be proven to be the supplier (missing, archived, other VAT or other
            # company). Never fall back to selecting the child contact row.
            return _result(
                status=PartnerMatchStatus.MULTIPLE_MATCHES,
                partner_id=None,
                matched_by=None,
                reason="The commercial partner of the matching supplier contacts failed validation.",
                candidate_count=len(group.contacts),
                confidence=None,
            )
        return _result(
            status=PartnerMatchStatus.MATCHED,
            partner_id=group.commercial_partner_id,
            matched_by="tax_number",
            reason="Unique supplier partner match by tax number.",
            candidate_count=1,
            confidence=EXACT_MATCH_CONFIDENCE,
        )


@dataclass(frozen=True, slots=True)
class CommercialPartnerGroup:
    """Active exact-VAT rows that share one canonical commercial partner.

    ``partner`` is the canonical commercial partner's own record as read from Odoo
    (unvalidated), or ``None`` when it could not be read.
    """

    commercial_partner_id: int
    partner: Partner | None
    contacts: tuple[Partner, ...]


def canonical_partner_id(partner: Partner) -> int:
    """``commercial_partner_id`` when Odoo supplied it, otherwise the row's own id."""
    return partner.commercial_partner_id if partner.commercial_partner_id is not None else partner.id


def group_by_commercial_partner(
    candidates: Sequence[Partner],
    repository: PartnerRepository,
) -> tuple[CommercialPartnerGroup, ...]:
    """Group rows by canonical commercial partner, in first-seen order.

    A canonical partner absent from ``candidates`` is read by id (one batch call).
    """
    contacts_by_id: dict[int, list[Partner]] = {}
    for candidate in candidates:
        contacts_by_id.setdefault(canonical_partner_id(candidate), []).append(candidate)
    rows_by_id = {candidate.id: candidate for candidate in candidates}
    missing = tuple(partner_id for partner_id in contacts_by_id if partner_id not in rows_by_id)
    loaded = {partner.id: partner for partner in repository.find_by_ids(missing)} if missing else {}
    return tuple(
        CommercialPartnerGroup(
            commercial_partner_id=partner_id,
            partner=rows_by_id.get(partner_id) or loaded.get(partner_id),
            contacts=tuple(contacts),
        )
        for partner_id, contacts in contacts_by_id.items()
    )


def _is_valid_canonical_partner(group: CommercialPartnerGroup, *, tax_number: str, company_id: int | None) -> bool:
    partner = group.partner
    if partner is None or partner.id != group.commercial_partner_id:
        return False
    if canonical_partner_id(partner) != partner.id or not partner.active:
        return False
    if _clean(partner.tax_number) != tax_number:
        return False
    return company_id is None or partner.company_id in (company_id, None)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _active_candidates(candidates: Sequence[Partner]) -> tuple[Partner, ...]:
    return tuple(candidate for candidate in candidates if candidate.active)


def _result(
    *,
    status: PartnerMatchStatus,
    partner_id: int | None,
    matched_by: str | None,
    reason: str,
    candidate_count: int,
    confidence: Decimal | None,
) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id if status == PartnerMatchStatus.MATCHED else None,
        matched_by=matched_by,
        reason=reason,
        candidate_count=candidate_count,
        confidence=confidence,
    )
