"""UBL-TR exchange-rate facts for the foreign-currency Vendor Bill rate (P1).

Extraction runs against the original stored UBL document, never against
:class:`~app.domain.invoice.dto.InternalInvoice`: persisted source-invoice evidence is
fingerprinted on replay and carries only the first ``CalculationRate`` value, without its
``SourceCurrencyCode`` / ``TargetCurrencyCode``, so adding the direction there would turn
every replay into an idempotency conflict (same reasoning as
:mod:`app.domain.invoice.tax_facts` and :mod:`app.domain.invoice.billing_reference`).

Every invoice-level ``*ExchangeRate`` element is recorded -- matched on local names, so a
foreign namespace cannot hide one -- with its currency codes and its rate exactly as
written. Which one is the accounting rate is decided by the rate-selection rule
(:func:`app.billing.fx_rate.select_vendor_bill_rate`), not here. Extraction never raises
for a malformed rate: it is recorded with ``calculation_rate=None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from app.domain.invoice.parser import parse_ubl_invoice_root

PRICING_EXCHANGE_RATE = "PricingExchangeRate"


@dataclass(frozen=True, slots=True)
class SourceExchangeRateFact:
    element: str
    source_currency: str | None
    target_currency: str | None
    #: The ``CalculationRate`` text exactly as written; None when absent.
    calculation_rate_text: str | None
    #: The exact Decimal of that text; None when absent or not a finite number.
    calculation_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class SourceFxFacts:
    invoice_uuid: str | None
    document_currency: str | None
    exchange_rates: tuple[SourceExchangeRateFact, ...] = ()

    def by_element(self, element: str) -> tuple[SourceExchangeRateFact, ...]:
        return tuple(rate for rate in self.exchange_rates if rate.element == element)


def extract_source_fx_facts(content: bytes | str) -> SourceFxFacts:
    """Raises :class:`~app.domain.invoice.exceptions.InvoiceDomainError` subclasses for
    malformed, unsafe or non-invoice XML -- the same rules as the canonical parser."""

    root = parse_ubl_invoice_root(content)
    return SourceFxFacts(
        invoice_uuid=_child_text(root, "UUID"),
        document_currency=_upper(_child_text(root, "DocumentCurrencyCode")),
        exchange_rates=tuple(
            _exchange_rate_fact(child) for child in root if _local_name(child.tag).endswith("ExchangeRate")
        ),
    )


def _exchange_rate_fact(element: ElementTree.Element) -> SourceExchangeRateFact:
    rate_text = _child_text(element, "CalculationRate")
    return SourceExchangeRateFact(
        element=_local_name(element.tag),
        source_currency=_upper(_child_text(element, "SourceCurrencyCode")),
        target_currency=_upper(_child_text(element, "TargetCurrencyCode")),
        calculation_rate_text=rate_text,
        calculation_rate=_decimal(rate_text),
    )


def _child_text(element: ElementTree.Element, local_name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == local_name:
            text = (child.text or "").strip()
            return text or None
    return None


def _decimal(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _upper(value: str | None) -> str | None:
    return value.upper() if value is not None else None


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]
