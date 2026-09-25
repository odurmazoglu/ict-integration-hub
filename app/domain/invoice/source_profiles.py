"""Supplier-specific source semantics for authoritative product identities.

P0-PROD-19A-2. A *source profile* says where one known supplier places an
authoritative manufacturer SKU in its UBL when ``ManufacturersItemIdentification``
is absent. Profiles are keyed on the invoice supplier's tax number (the same exact
identity ``PartnerMatchingEngine`` matches on), never on names.

Only one profile exists: VİTEL (VKN 9250020961), ICT's ManageEngine distributor,
puts the ManageEngine SKU as the *whole* ``cac:Item/cbc:Description``. The value is
accepted only when:

- the supplier VKN is exactly ``9250020961``;
- the line description provably came from ``cbc:Description`` (not the ``cbc:Name``
  fallback, and not evidence of unknown provenance);
- the entire trimmed value is a strict ManageEngine SKU (``MANAGEENGINE_SKU_PATTERN``).

No substring search, no prose extraction, no case folding. Description is never a
product identifier for any other supplier.
"""

from __future__ import annotations

import re

from app.domain.invoice.dto import DESCRIPTION_SOURCE_DESCRIPTION, InternalInvoice, InvoiceLine

VITEL_SUPPLIER_VKN = "9250020961"

# ManageEngine variant SKUs observed in ICT product master data, e.g. 85710.1S1,
# 85710.0MS5, 6705.5MEE, 67005.6MPMFA4, 702012.0SUP6: a 4-6 digit product number, a dot,
# then a digit followed by 1-9 uppercase alphanumerics. Always applied with fullmatch.
MANAGEENGINE_SKU_PATTERN = re.compile(r"[0-9]{4,6}\.[0-9][0-9A-Z]{1,9}")


def is_manageengine_sku(value: str) -> bool:
    return MANAGEENGINE_SKU_PATTERN.fullmatch(value) is not None


def profile_manufacturer_sku(invoice: InternalInvoice, line: InvoiceLine) -> str | None:
    """The authoritative manufacturer SKU a supplier profile reads from ``line``, if any."""

    supplier_vkn = invoice.supplier.tax_number
    if supplier_vkn is None or supplier_vkn.strip() != VITEL_SUPPLIER_VKN:
        return None
    if line.description_source != DESCRIPTION_SOURCE_DESCRIPTION or line.description is None:
        return None
    candidate = line.description.strip()
    return candidate if is_manageengine_sku(candidate) else None
