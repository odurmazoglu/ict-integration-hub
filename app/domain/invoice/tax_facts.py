"""UBL-TR source tax facts for the withholding-tax safety guard (PR T W1).

Extraction runs against the original stored UBL document, never against
:class:`~app.domain.invoice.dto.InternalInvoice`: the canonical parser ignores
``cbc:TaxTypeCode`` and ``WithholdingTaxTotal`` entirely, and persisted source-invoice
evidence is fingerprinted on replay, so adding a field there would turn every replay
into an idempotency conflict (same reasoning as :mod:`app.domain.invoice.billing_reference`).

Detection is deliberately broad, so it can only ever over-block:

* every ``WithholdingTaxTotal`` element counts, in any namespace and at any depth --
  a direct child of ``Invoice`` is invoice-level, anything deeper is line-level;
* ``TaxTypeCode`` is collected from every ``TaxSubtotal`` outside a withholding total
  (invoice-level ``TaxTotal`` and every line), matched on local names; a subtotal
  without a code is counted, never assumed to be VAT.

PR T T2-A adds the per-subtotal facts the VAT exemption policy needs
(:class:`SourceTaxSubtotalFact`). Unlike the broad detection above they follow the
canonical parser's traversal *exactly* -- the same namespaced ``findall`` paths and the
same flattening of ``TaxTotal`` / ``TaxSubtotal`` into ``(line, tax_index)`` positions --
so a fact can be aligned one-to-one with the canonical invoice's taxes. Their extraction
never raises: an unparseable number is recorded as ``malformed`` and only the exemption
policy (which fails closed on it) reads it, so W1's verdict for a document is unchanged.

The XML safety rules (no DTD / entities, UBL ``Invoice`` root) are the canonical
parser's own (:func:`~app.domain.invoice.parser.parse_ubl_invoice_root`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from app.domain.invoice.parser import NS, parse_ubl_invoice_root

#: GİB ``InvoiceTypeCode`` values that declare tax withholding (tevkifat). The return
#: variants are listed too; the return-invoice guard takes precedence over them anyway.
WITHHOLDING_INVOICE_TYPE_CODES = frozenset({"TEVKIFAT", "YTBTEVKIFAT", "TEVKIFATIADE", "YTBTEVKIFATIADE"})
#: The only ``TaxTypeCode`` a Vendor Bill may be built from today: 0015 (KDV / VAT).
SUPPORTED_TAX_TYPE_CODES = frozenset({"0015"})

_WITHHOLDING_TAX_TOTAL = "WithholdingTaxTotal"
_TAX_SUBTOTAL = "TaxSubtotal"
_TAX_TYPE_CODE_PATH = ("TaxCategory", "TaxScheme", "TaxTypeCode")


@dataclass(frozen=True, slots=True)
class SourceTaxSubtotalFact:
    """One canonical-parser tax position of the original document (PR T T2-A).

    ``line_id`` is the ``cac:InvoiceLine/cbc:ID`` text (``None`` for an invoice-level
    ``cac:TaxTotal``); ``tax_index`` is the position the canonical parser gives the tax
    within that line. ``has_subtotal`` is ``False`` for a ``TaxTotal`` without any
    ``TaxSubtotal`` (the parser's amount-only placeholder tax).
    """

    line_id: str | None
    tax_index: int
    has_subtotal: bool
    tax_type_code: str | None
    exemption_reason_code: str | None
    exemption_reason: str | None
    percent_present: bool
    percent: Decimal | None
    tax_amount: Decimal | None
    taxable_amount: Decimal | None
    #: A numeric element is present but is not a valid decimal.
    malformed: bool = False


@dataclass(frozen=True, slots=True)
class SourceTaxFacts:
    """Tax facts of one original UBL invoice document."""

    invoice_uuid: str | None
    invoice_type_code: str | None
    #: Distinct, sorted ``TaxTypeCode`` values of every non-withholding ``TaxSubtotal``.
    tax_type_codes: tuple[str, ...]
    tax_subtotals_without_type_code: int
    invoice_withholding_total_count: int
    line_withholding_total_count: int
    #: PR T T2-A. ``cbc:ID`` of every ``cac:InvoiceLine`` in document order; ``None`` when
    #: the structure was not extracted (the exemption policy then fails closed).
    line_ids: tuple[str | None, ...] | None = None
    line_tax_subtotals: tuple[SourceTaxSubtotalFact, ...] = ()
    invoice_tax_subtotals: tuple[SourceTaxSubtotalFact, ...] = ()

    @property
    def withholding_invoice_type(self) -> bool:
        return self.invoice_type_code in WITHHOLDING_INVOICE_TYPE_CODES

    @property
    def has_withholding(self) -> bool:
        return (
            self.withholding_invoice_type
            or self.invoice_withholding_total_count > 0
            or self.line_withholding_total_count > 0
        )

    @property
    def unsupported_tax_type_codes(self) -> tuple[str, ...]:
        return tuple(code for code in self.tax_type_codes if code not in SUPPORTED_TAX_TYPE_CODES)

    @property
    def has_unsupported_tax_content(self) -> bool:
        return bool(self.unsupported_tax_type_codes) or self.tax_subtotals_without_type_code > 0


def extract_source_tax_facts(content: bytes | str) -> SourceTaxFacts:
    """The tax facts of a UBL invoice document.

    Raises :class:`~app.domain.invoice.exceptions.InvoiceDomainError` subclasses for
    malformed, unsafe or non-invoice XML -- the same rules as the canonical parser.
    """

    root = parse_ubl_invoice_root(content)
    invoice_level = sum(1 for child in root if _local_name(child.tag) == _WITHHOLDING_TAX_TOTAL)
    all_levels = sum(1 for element in root.iter() if _local_name(element.tag) == _WITHHOLDING_TAX_TOTAL)
    codes: set[str] = set()
    missing = 0
    for subtotal in _non_withholding_tax_subtotals(root):
        code = _tax_type_code(subtotal)
        if code is None:
            missing += 1
        else:
            codes.add(code)
    lines = root.findall("cac:InvoiceLine", NS)
    line_ids = tuple(_text(line.find("cbc:ID", NS)) for line in lines)
    return SourceTaxFacts(
        invoice_uuid=_text(root.find("cbc:UUID", NS)),
        invoice_type_code=_canonical(_text(root.find("cbc:InvoiceTypeCode", NS))),
        tax_type_codes=tuple(sorted(codes)),
        tax_subtotals_without_type_code=missing,
        invoice_withholding_total_count=invoice_level,
        line_withholding_total_count=all_levels - invoice_level,
        line_ids=line_ids,
        line_tax_subtotals=tuple(
            fact for line, line_id in zip(lines, line_ids, strict=True) for fact in _tax_position_facts(line, line_id)
        ),
        invoice_tax_subtotals=_tax_position_facts(root, None),
    )


def _tax_position_facts(element: ElementTree.Element, line_id: str | None) -> tuple[SourceTaxSubtotalFact, ...]:
    """Mirror of the canonical parser's ``_taxes``: every ``cac:TaxTotal`` contributes its
    ``cac:TaxSubtotal`` children in order, or one placeholder when it has none."""

    facts: list[SourceTaxSubtotalFact] = []
    for total in element.findall("cac:TaxTotal", NS):
        subtotals = total.findall("cac:TaxSubtotal", NS)
        if not subtotals:
            amount, amount_malformed = _decimal(total, "cbc:TaxAmount")
            facts.append(
                SourceTaxSubtotalFact(
                    line_id=line_id,
                    tax_index=len(facts),
                    has_subtotal=False,
                    tax_type_code=None,
                    exemption_reason_code=None,
                    exemption_reason=None,
                    percent_present=False,
                    percent=None,
                    tax_amount=amount,
                    taxable_amount=None,
                    malformed=amount_malformed,
                )
            )
            continue
        for subtotal in subtotals:
            facts.append(_subtotal_fact(subtotal, line_id=line_id, tax_index=len(facts)))
    return tuple(facts)


def _subtotal_fact(subtotal: ElementTree.Element, *, line_id: str | None, tax_index: int) -> SourceTaxSubtotalFact:
    category = subtotal.find("cac:TaxCategory", NS)
    percent, percent_malformed = _decimal(subtotal, "cbc:Percent")
    tax_amount, tax_amount_malformed = _decimal(subtotal, "cbc:TaxAmount")
    taxable_amount, taxable_malformed = _decimal(subtotal, "cbc:TaxableAmount")
    return SourceTaxSubtotalFact(
        line_id=line_id,
        tax_index=tax_index,
        has_subtotal=True,
        tax_type_code=_text(category.find("cac:TaxScheme/cbc:TaxTypeCode", NS)) if category is not None else None,
        exemption_reason_code=_text(category.find("cbc:TaxExemptionReasonCode", NS)) if category is not None else None,
        exemption_reason=_text(category.find("cbc:TaxExemptionReason", NS)) if category is not None else None,
        percent_present=subtotal.find("cbc:Percent", NS) is not None,
        percent=percent,
        tax_amount=tax_amount,
        taxable_amount=taxable_amount,
        malformed=percent_malformed or tax_amount_malformed or taxable_malformed,
    )


def _decimal(element: ElementTree.Element, path: str) -> tuple[Decimal | None, bool]:
    """``(value, malformed)``; an absent or empty element is ``(None, False)``, like the parser."""

    text = _text(element.find(path, NS))
    if text is None:
        return None, False
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, True
    if not value.is_finite():
        return None, True
    return value, False


def _non_withholding_tax_subtotals(element: ElementTree.Element) -> Iterator[ElementTree.Element]:
    for child in element:
        local_name = _local_name(child.tag)
        if local_name == _WITHHOLDING_TAX_TOTAL:
            continue
        if local_name == _TAX_SUBTOTAL:
            yield child
        yield from _non_withholding_tax_subtotals(child)


def _tax_type_code(subtotal: ElementTree.Element) -> str | None:
    current: ElementTree.Element | None = subtotal
    for local_name in _TAX_TYPE_CODE_PATH:
        current = _child(current, local_name) if current is not None else None
    return _text(current)


def _child(element: ElementTree.Element, local_name: str) -> ElementTree.Element | None:
    return next((child for child in element if _local_name(child.tag) == local_name), None)


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _text(element: ElementTree.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _canonical(value: str | None) -> str | None:
    return value.upper() if value is not None else None
