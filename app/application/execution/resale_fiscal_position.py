"""Fiscal-position safety for a RESALE Vendor Bill's pinned account (P0-PROD-18F-2).

The RESALE pin (P0-PROD-18F-1) freezes the *pre-fiscal-position* account. Before that
account is sent to Odoo explicitly, this proves Odoo's fiscal-position configuration
cannot remap it. It is deliberately *not* a second accounting engine: it never decides
which fiscal position applies and never computes a substitute account. It only answers
"can any fiscal position that could reach this bill map the pinned account?" -- and
fails closed on anything it cannot prove.

Fiscal positions that could reach a Vendor Bill created by the Hub (the Hub never sets
``fiscal_position_id`` itself; Odoo computes it from the supplier):

* the supplier partner's explicit ``property_account_position_id`` -- Odoo lets it win
  over automatic detection;
* every active, automatically applicable (``auto_apply``) fiscal position in the
  company's scope. Which one Odoo would pick depends on country/state/zip/VAT rules this
  module intentionally does not re-implement, so *all* of them are treated as possibly
  applicable.

The pinned account is safe only when no mapping of any of those fiscal positions has it
as its source account (a same-account mapping is still treated as a remap: v1 proves
"no mapping touches it", nothing weaker).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.execution.exceptions import ResaleExecutionAccountingError

#: Upper bounds for the reads below; larger results are refused, never truncated.
MAX_FISCAL_POSITIONS = 200
MAX_FISCAL_POSITION_ACCOUNT_MAPPINGS = 2000


@dataclass(frozen=True, slots=True)
class PartnerFiscalPositionRecord(ApplicationDTO):
    """A supplier partner and its explicit (company-dependent) fiscal position, if any."""

    partner_id: int
    company_id: int | None
    fiscal_position_id: int | None


@dataclass(frozen=True, slots=True)
class FiscalPositionRecord(ApplicationDTO):
    id: int
    active: bool
    company_id: int | None
    auto_apply: bool
    #: ``account.fiscal.position.account`` ids Odoo reports for this position.
    account_mapping_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class FiscalPositionAccountMappingRecord(ApplicationDTO):
    id: int
    position_id: int
    account_src_id: int
    account_dest_id: int


class FiscalPositionReader(Protocol):
    """Structurally read-only port; the adapter owns every model, domain and field."""

    def accessible_company_ids(self) -> tuple[int, ...]:
        """Every Odoo company visible to the integration user, ascending."""

    def find_partner(self, *, company_id: int, partner_id: int) -> PartnerFiscalPositionRecord | None:
        pass

    def find_fiscal_position(self, *, fiscal_position_id: int) -> FiscalPositionRecord | None:
        """The fiscal position (active or archived), or ``None`` if not readable."""

    def list_auto_apply_fiscal_positions(self, *, company_id: int) -> tuple[FiscalPositionRecord, ...]:
        """Every active ``auto_apply`` fiscal position shared or owned by ``company_id``."""

    def list_account_mappings(
        self, *, fiscal_position_ids: tuple[int, ...]
    ) -> tuple[FiscalPositionAccountMappingRecord, ...]:
        pass


class FiscalPositionSafetyBlocker(StrEnum):
    COMPANY_CONTEXT_UNVERIFIED = "company_context_unverified"
    PARTNER_NOT_FOUND = "partner_not_found"
    PARTNER_COMPANY_MISMATCH = "partner_company_mismatch"
    EXPLICIT_FISCAL_POSITION_UNREADABLE = "explicit_fiscal_position_unreadable"
    FISCAL_POSITION_OUT_OF_SCOPE = "fiscal_position_out_of_scope"
    ACCOUNT_MAPPINGS_INCONSISTENT = "account_mappings_inconsistent"
    PINNED_ACCOUNT_MAPPED = "pinned_account_mapped"


@dataclass(frozen=True, slots=True)
class FiscalPositionSafety(ApplicationDTO):
    """Which fiscal positions were considered, and whether the pinned accounts are safe."""

    considered_fiscal_position_ids: tuple[int, ...]
    explicit_fiscal_position_id: int | None
    remapping_fiscal_position_ids: tuple[int, ...] = field(default_factory=tuple)
    blockers: tuple[FiscalPositionSafetyBlocker, ...] = field(default_factory=tuple)

    @property
    def safe(self) -> bool:
        return not self.blockers


def evaluate_fiscal_position_safety(
    *,
    company_id: int,
    partner: PartnerFiscalPositionRecord,
    explicit_position: FiscalPositionRecord | None,
    auto_positions: tuple[FiscalPositionRecord, ...],
    mappings: tuple[FiscalPositionAccountMappingRecord, ...],
    pinned_account_ids: frozenset[int],
) -> FiscalPositionSafety:
    """Pure rule: safe only if no possibly-applicable fiscal position maps a pinned account."""

    blockers: list[FiscalPositionSafetyBlocker] = []
    if partner.company_id not in (None, company_id):
        blockers.append(FiscalPositionSafetyBlocker.PARTNER_COMPANY_MISMATCH)
    considered: dict[int, FiscalPositionRecord] = {}
    for position in auto_positions:
        if not position.active or not position.auto_apply or position.company_id not in (None, company_id):
            blockers.append(FiscalPositionSafetyBlocker.FISCAL_POSITION_OUT_OF_SCOPE)
        considered[position.id] = position
    if partner.fiscal_position_id is not None:
        if explicit_position is None or explicit_position.id != partner.fiscal_position_id:
            blockers.append(FiscalPositionSafetyBlocker.EXPLICIT_FISCAL_POSITION_UNREADABLE)
        elif explicit_position.company_id not in (None, company_id):
            blockers.append(FiscalPositionSafetyBlocker.FISCAL_POSITION_OUT_OF_SCOPE)
        else:
            considered[explicit_position.id] = explicit_position

    # Every mapping Odoo reports on the considered positions must be exactly the set read,
    # so a mapping hidden from the mapping read cannot silently escape the check.
    expected_mapping_ids = {
        mapping_id for position in considered.values() for mapping_id in position.account_mapping_ids
    }
    read_mapping_ids = [mapping.id for mapping in mappings]
    if (
        len(set(read_mapping_ids)) != len(read_mapping_ids)
        or set(read_mapping_ids) != expected_mapping_ids
        or any(considered.get(mapping.position_id) is None for mapping in mappings)
        or any(mapping.id not in considered[mapping.position_id].account_mapping_ids for mapping in mappings)
    ):
        blockers.append(FiscalPositionSafetyBlocker.ACCOUNT_MAPPINGS_INCONSISTENT)

    remapping = tuple(
        sorted({mapping.position_id for mapping in mappings if mapping.account_src_id in pinned_account_ids})
    )
    if remapping:
        blockers.append(FiscalPositionSafetyBlocker.PINNED_ACCOUNT_MAPPED)
    return FiscalPositionSafety(
        considered_fiscal_position_ids=tuple(sorted(considered)),
        explicit_fiscal_position_id=partner.fiscal_position_id,
        remapping_fiscal_position_ids=remapping,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def check_fiscal_position_safety(
    reader: FiscalPositionReader,
    *,
    company_id: int,
    partner_id: int,
    pinned_account_ids: frozenset[int],
) -> FiscalPositionSafety:
    """Read the evidence and apply :func:`evaluate_fiscal_position_safety`; raise unless safe.

    The partner's fiscal position is a company-dependent field, read in the integration
    user's Odoo company context -- provably ``company_id``'s only when it is the one and
    only company visible (the P0-PROD-18D rule).
    """

    if reader.accessible_company_ids() != (company_id,):
        raise _unsafe((FiscalPositionSafetyBlocker.COMPANY_CONTEXT_UNVERIFIED,))
    partner = reader.find_partner(company_id=company_id, partner_id=partner_id)
    if partner is None or partner.partner_id != partner_id:
        raise _unsafe((FiscalPositionSafetyBlocker.PARTNER_NOT_FOUND,))
    explicit_position = (
        reader.find_fiscal_position(fiscal_position_id=partner.fiscal_position_id)
        if partner.fiscal_position_id is not None
        else None
    )
    auto_positions = reader.list_auto_apply_fiscal_positions(company_id=company_id)
    position_ids = {position.id for position in auto_positions}
    if explicit_position is not None:
        position_ids.add(explicit_position.id)
    mappings = reader.list_account_mappings(fiscal_position_ids=tuple(sorted(position_ids))) if position_ids else ()
    safety = evaluate_fiscal_position_safety(
        company_id=company_id,
        partner=partner,
        explicit_position=explicit_position,
        auto_positions=auto_positions,
        mappings=mappings,
        pinned_account_ids=pinned_account_ids,
    )
    if not safety.safe:
        raise _unsafe(safety.blockers, remapping=safety.remapping_fiscal_position_ids)
    return safety


def _unsafe(
    blockers: tuple[FiscalPositionSafetyBlocker, ...], *, remapping: tuple[int, ...] = ()
) -> ResaleExecutionAccountingError:
    detail = ",".join(blocker.value for blocker in blockers)
    if remapping:
        detail += f" (fiscal positions {', '.join(str(position_id) for position_id in remapping)})"
    return ResaleExecutionAccountingError(
        "RESALE execution blocked: fiscal-position configuration cannot be proven to keep the pinned "
        f"account -- {detail}."
    )


__all__ = [
    "MAX_FISCAL_POSITIONS",
    "MAX_FISCAL_POSITION_ACCOUNT_MAPPINGS",
    "FiscalPositionAccountMappingRecord",
    "FiscalPositionReader",
    "FiscalPositionRecord",
    "FiscalPositionSafety",
    "FiscalPositionSafetyBlocker",
    "PartnerFiscalPositionRecord",
    "check_fiscal_position_safety",
    "evaluate_fiscal_position_safety",
]
