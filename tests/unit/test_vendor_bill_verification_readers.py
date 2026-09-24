from __future__ import annotations

from decimal import Decimal

import pytest

from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.account_move_line_verification_reader import (
    VENDOR_BILL_INVOICE_LINE_VERIFICATION_FIELDS,
    OdooAccountMoveLineVerificationReader,
)
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.vendor_bill_header_verification_reader import (
    VENDOR_BILL_HEADER_VERIFICATION_FIELDS,
    OdooVendorBillHeaderVerificationReader,
)


class _ReadOnlyClient:
    def __init__(self, records) -> None:
        self.records = list(records)
        self.calls = []

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        self.calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit, "offset": offset})
        return list(self.records)


def _adapter(records):
    client = _ReadOnlyClient(records)
    return OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0), client


def test_header_reader_uses_fixed_company_scoped_vendor_bill_query_and_preserves_decimals() -> None:
    adapter, client = _adapter(
        [
            {
                "id": 63,
                "company_id": [7, "ICT"],
                "state": "draft",
                "move_type": "in_invoice",
                "partner_id": [439, "Vendor"],
                "currency_id": [31, "TRY"],
                "amount_untaxed": "4959.80",
                "amount_tax": "991.96",
                "amount_total": "5951.76",
            }
        ]
    )
    result = OdooVendorBillHeaderVerificationReader(adapter=adapter).read_vendor_bill(move_id=63, company_id=7)

    assert client.calls == [
        {
            "model": "account.move",
            "domain": [["id", "=", 63], ["company_id", "=", 7], ["move_type", "=", "in_invoice"]],
            "fields": list(VENDOR_BILL_HEADER_VERIFICATION_FIELDS),
            "limit": 2,
            "offset": 0,
        }
    ]
    assert result is not None
    assert result.partner_id == 439
    assert result.currency == "TRY"
    assert result.amount_untaxed == Decimal("4959.80")
    assert result.amount_tax == Decimal("991.96")
    assert result.amount_total == Decimal("5951.76")


def test_header_reader_returns_none_when_scoped_bill_is_absent() -> None:
    adapter, _ = _adapter([])
    assert OdooVendorBillHeaderVerificationReader(adapter=adapter).read_vendor_bill(move_id=63, company_id=7) is None


def test_header_reader_fails_closed_on_ambiguous_or_cross_company_response() -> None:
    valid = {
        "id": 63,
        "company_id": [7, "ICT"],
        "state": "draft",
        "move_type": "in_invoice",
        "partner_id": [439, "Vendor"],
        "currency_id": [31, "TRY"],
        "amount_untaxed": "1",
        "amount_tax": "0.2",
        "amount_total": "1.2",
    }
    adapter, _ = _adapter([valid, valid])
    with pytest.raises(ErpRepositoryResponseError):
        OdooVendorBillHeaderVerificationReader(adapter=adapter).read_vendor_bill(move_id=63, company_id=7)
    adapter, _ = _adapter([{**valid, "company_id": [8, "Other"]}])
    with pytest.raises(ErpRepositoryResponseError):
        OdooVendorBillHeaderVerificationReader(adapter=adapter).read_vendor_bill(move_id=63, company_id=7)


def test_invoice_line_reader_preserves_account_only_identity_taxes_and_decimals() -> None:
    adapter, client = _adapter(
        [
            {
                "id": 101,
                "move_id": [63, "BILL/2026/63"],
                "product_id": False,
                "quantity": "20",
                "price_unit": "74.397000",
                "tax_ids": [34],
                "account_id": [247, "Expense"],
                "price_subtotal": "1487.94",
                "price_total": "1785.53",
            }
        ]
    )
    (line,) = OdooAccountMoveLineVerificationReader(adapter=adapter).read_invoice_lines_for_move(move_id=63)

    assert client.calls[0]["model"] == "account.move.line"
    assert client.calls[0]["domain"] == [["move_id", "=", 63], ["display_type", "=", "product"]]
    assert client.calls[0]["fields"] == list(VENDOR_BILL_INVOICE_LINE_VERIFICATION_FIELDS)
    assert line.product_id is None
    assert line.account_id == 247
    assert line.tax_ids == (34,)
    assert line.quantity == Decimal("20")
    assert line.price_unit == Decimal("74.397000")
    assert line.price_subtotal == Decimal("1487.94")
    assert line.price_total == Decimal("1785.53")
    for method in ("create", "write", "unlink", "action_post", "payment", "reversal"):
        assert not hasattr(client, method)
