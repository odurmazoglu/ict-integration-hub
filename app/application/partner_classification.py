"""ICT business-relationship classification for Hub-written Odoo supplier partners.

The classification is a *partner master-data* attribute only (in production the
Studio selection ``res.partner.x_studio_musteri_tipi``). It never determines
purchase purpose, expense account, accounting/tax/product treatment or the
Vendor Bill workflow -- those stay review/invoice-level Hub decisions.

The Hub only ever *sets* a classification on a partner it is creating in the same
call. It never changes the classification of an existing partner: an existing
value is evaluated deterministically by :func:`evaluate_existing_classification`
and surfaced, never overwritten. A production ``ir.default`` (``customer``) is
deliberately not relied upon -- every Hub create carries an explicit value.
"""

from __future__ import annotations

from enum import StrEnum


class SupplierPartnerClassification(StrEnum):
    """Stable Hub classification keys; equal to the Odoo selection technical keys."""

    #: A normal operational/commercial supplier (CREATE_PERMANENT_SUPPLIER).
    VENDOR = "vendor"
    #: A supplier that exists primarily because ICT incurred an incidental or
    #: operating expense with that legal entity (ONE_OFF_VENDOR).
    EXPENSE_VENDOR = "expense_vendor"


class PartnerClassificationOutcome(StrEnum):
    """What happened to the partner classification during one supplier write."""

    #: The Hub created the partner in this call with the target classification.
    CLASSIFIED_ON_CREATE = "classified_on_create"
    #: The existing partner already carries exactly the target classification.
    ALREADY_CLASSIFIED = "already_classified"
    #: The existing partner has no classification; preserved (no write). An
    #: operator classifies it explicitly in Odoo.
    UNCLASSIFIED_PRESERVED = "unclassified_preserved"
    #: The existing partner has a different classification (customer, vendor,
    #: partner, Karma, prospect, ...); preserved (no write) and surfaced.
    DIFFERENT_CLASSIFICATION_PRESERVED = "different_classification_preserved"


def evaluate_existing_classification(
    current_value: str | None,
    *,
    target: SupplierPartnerClassification,
) -> PartnerClassificationOutcome:
    """Deterministic, write-free decision for a partner the Hub did not create in this call.

    Never returns a "change it" outcome: the Hub has no capability to rewrite an
    existing partner's classification, so ambiguity is always resolved by
    preserving the existing value and surfacing the condition.
    """

    if not isinstance(target, SupplierPartnerClassification):
        raise ValueError("A canonical SupplierPartnerClassification target is required.")
    normalized = normalize_classification_value(current_value)
    if normalized is None:
        return PartnerClassificationOutcome.UNCLASSIFIED_PRESERVED
    if normalized == target.value:
        return PartnerClassificationOutcome.ALREADY_CLASSIFIED
    return PartnerClassificationOutcome.DIFFERENT_CLASSIFICATION_PRESERVED


def normalize_classification_value(value: object) -> str | None:
    """Odoo returns ``False`` for an empty selection; never fold case or rewrite keys
    (the production ``Karma`` key is deliberately left exactly as stored)."""

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


#: Outcomes an operator must look at (the classification is not the intended one).
ATTENTION_OUTCOMES = frozenset(
    {
        PartnerClassificationOutcome.UNCLASSIFIED_PRESERVED,
        PartnerClassificationOutcome.DIFFERENT_CLASSIFICATION_PRESERVED,
    }
)


__all__ = [
    "ATTENTION_OUTCOMES",
    "PartnerClassificationOutcome",
    "SupplierPartnerClassification",
    "evaluate_existing_classification",
    "normalize_classification_value",
]
