"""Create-time safety guard: is there probably already an Odoo company for this supplier?

VAT stays the only deterministic supplier identity. This module never *matches* a
supplier by name; it only answers whether creating a new supplier partner could
duplicate an existing commercial company whose stored VAT is missing or is not a
valid VKN/TCKN (legacy customer codes such as ``983551``).

Name canonicalization (exact, no similarity scoring):

1. NFKC, then Turkish-aware folding (``İ ı Ş Ğ Ü Ö Ç`` -> ``I I S G U O C``), then
   uppercase and removal of any remaining combining marks.
2. Every non-alphanumeric character becomes a separator (``"TİC.AŞ."`` -> ``TIC AS``).
3. Whole-token abbreviation expansion: ``SAN``->``SANAYI``, ``TIC``->``TICARET``,
   ``MAK``->``MAKINA``, ``LTD``->``LIMITED``, ``STI``->``SIRKETI``,
   ``AS`` / ``A S`` -> ``ANONIM SIRKETI``.
4. Trailing legal-form tokens (``ANONIM``, ``LIMITED``, ``SIRKETI``, ``SIRKET``,
   ``KOLLEKTIF``, ``KOMANDIT``) are removed from the end.
5. The generic descriptors ``VE``, ``SANAYI`` and ``TICARET`` are removed anywhere,
   because e-invoices routinely abbreviate or omit "San. ve Tic.".
6. The remaining tokens form the core. A core with fewer than two tokens or fewer
   than six letters/digits is too weak to compare and never blocks a create.

A candidate blocks the create only when ALL hold: active; ``is_company``; no parent
(a commercial head, so child contacts never count); its VAT differs from the incoming
VAT and is missing or not a valid VKN/TCKN (a different *valid* VAT may be a genuinely
different legal entity); and its canonical core is exactly equal to the incoming
supplier's canonical core.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.invoice.party_tax_identity import is_valid_party_tax_identifier

_TURKISH_FOLD = str.maketrans(
    {
        "İ": "I",
        "ı": "I",
        "i": "I",
        "Ş": "S",
        "ş": "S",
        "Ğ": "G",
        "ğ": "G",
        "Ü": "U",
        "ü": "U",
        "Ö": "O",
        "ö": "O",
        "Ç": "C",
        "ç": "C",
    }
)
_ABBREVIATIONS = {
    "SAN": ("SANAYI",),
    "TIC": ("TICARET",),
    "MAK": ("MAKINA",),
    "LTD": ("LIMITED",),
    "STI": ("SIRKETI",),
    "AS": ("ANONIM", "SIRKETI"),
}
_LEGAL_FORM_TAIL = frozenset({"ANONIM", "LIMITED", "SIRKETI", "SIRKET", "KOLLEKTIF", "KOMANDIT"})
_GENERIC_DESCRIPTORS = frozenset({"VE", "SANAYI", "TICARET"})
_MIN_CORE_TOKENS = 2
_MIN_CORE_CHARACTERS = 6


@dataclass(frozen=True, slots=True)
class ExistingCompanyRecord:
    """Read-only view of one Odoo ``res.partner`` considered by the create guard."""

    partner_id: int
    name: str | None
    vat: str | None
    active: bool
    is_company: bool
    parent_id: int | None


def canonical_company_name(value: str | None) -> str | None:
    """Canonical core of a company legal name, or ``None`` when too weak to compare."""

    if not value:
        return None
    folded = unicodedata.normalize("NFKC", value).translate(_TURKISH_FOLD).upper()
    folded = "".join(char for char in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(char))
    tokens = _expand(re.sub(r"[^A-Z0-9]+", " ", folded).split())
    while tokens and tokens[-1] in _LEGAL_FORM_TAIL:
        tokens.pop()
    core = [token for token in tokens if token not in _GENERIC_DESCRIPTORS]
    if len(core) < _MIN_CORE_TOKENS or sum(len(token) for token in core) < _MIN_CORE_CHARACTERS:
        return None
    return " ".join(core)


def probable_existing_companies(
    *,
    supplier_name: str | None,
    supplier_tax_number: str | None,
    candidates: Iterable[ExistingCompanyRecord],
) -> tuple[ExistingCompanyRecord, ...]:
    """Commercial companies that may already be this supplier under missing/invalid VAT."""

    source_core = canonical_company_name(supplier_name)
    if source_core is None:
        return ()
    source_vat = (supplier_tax_number or "").strip()
    found = {
        candidate.partner_id: candidate
        for candidate in candidates
        if _is_legacy_vat_company(candidate, source_vat=source_vat)
        and canonical_company_name(candidate.name) == source_core
    }
    return tuple(found[partner_id] for partner_id in sorted(found))


def _is_legacy_vat_company(candidate: ExistingCompanyRecord, *, source_vat: str) -> bool:
    if type(candidate.partner_id) is not int or candidate.partner_id <= 0:
        return False
    if not (candidate.active and candidate.is_company and candidate.parent_id is None):
        return False
    vat = (candidate.vat or "").strip()
    return vat != source_vat and not is_valid_party_tax_identifier(vat or None)


def _expand(tokens: list[str]) -> list[str]:
    expanded: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index] == "A" and index + 1 < len(tokens) and tokens[index + 1] == "S":
            expanded.extend(_ABBREVIATIONS["AS"])
            index += 2
            continue
        expanded.extend(_ABBREVIATIONS.get(tokens[index], (tokens[index],)))
        index += 1
    return expanded


__all__ = ["ExistingCompanyRecord", "canonical_company_name", "probable_existing_companies"]
