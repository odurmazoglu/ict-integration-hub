from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.billing import CustomerInvoice, CustomerInvoiceLine, VendorBill, VendorBillLine
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.erp.write import (
    AccountMoveRepository,
    CustomerInvoiceWriteDuplicateError,
    CustomerInvoiceWriteValidationError,
    VendorBillWriteAuthenticationError,
    VendorBillWriteAuthorizationError,
    VendorBillWriteDuplicateError,
    VendorBillWriteTransportError,
    VendorBillWriteUnexpectedErpError,
    VendorBillWriteValidationError,
)


async def test_account_move_repository_creates_draft_vendor_bill_payload() -> None:
    client = FakeJson2Client(create_result=7001)
    repository = AccountMoveRepository(client=client)

    result = await repository.create_draft_vendor_bill(vendor_bill=_vendor_bill(), idempotency_key="ettn-1")

    assert result.id == 7001
    assert client.create_calls == [
        {
            "move_type": "in_invoice",
            "company_id": 7,
            "partner_id": 101,
            "invoice_date": "2026-07-30",
            "ref": "INV-1",
            "currency": "TRY",
            "invoice_line_ids": (
                (
                    0,
                    0,
                    {
                        "product_id": 501,
                        "quantity": "2",
                        "price_unit": "10.50",
                        "tax_ids": ((6, 0, (401,)),),
                        "name": "Line 1",
                        "product_uom_id": "NIU",
                    },
                ),
            ),
            "narration": "safe note",
            "invoice_origin": "ettn-1",
        }
    ]
    assert "action_post" not in str(client.create_calls)
    assert "account.payment" not in str(client.create_calls)
    assert "unlink" not in str(client.create_calls)


async def test_account_move_repository_finds_existing_vendor_bill_by_idempotency_key() -> None:
    client = FakeJson2Client(search_records=[{"id": 7001, "name": "BILL/2026/001"}])
    repository = AccountMoveRepository(client=client)

    result = await repository.find_existing_vendor_bill(vendor_bill=_vendor_bill(), idempotency_key="ettn-1")

    assert result is not None
    assert result.id == 7001
    assert result.name == "BILL/2026/001"
    assert client.search_calls == [
        {
            "model": "account.move",
            "domain": [
                ["move_type", "=", "in_invoice"],
                ["invoice_origin", "=", "ettn-1"],
                ["company_id", "=", 7],
                ["partner_id", "=", 101],
            ],
            "fields": ["id", "name", "move_type", "invoice_origin", "company_id", "partner_id"],
            "limit": 2,
            "offset": 0,
        }
    ]


async def test_account_move_repository_returns_none_when_duplicate_not_found() -> None:
    repository = AccountMoveRepository(client=FakeJson2Client(search_records=[]))

    assert await repository.find_existing_vendor_bill(vendor_bill=_vendor_bill(), idempotency_key="ettn-1") is None


async def test_account_move_repository_scopes_vendor_bill_lookup_by_company_id() -> None:
    client = FakeJson2Client(search_records=[{"id": 7001, "name": "BILL/2026/001"}])
    repository = AccountMoveRepository(client=client)

    await repository.find_existing_vendor_bill(
        vendor_bill=_vendor_bill(),
        idempotency_key="ettn-1",
        company_id=9,
    )

    assert client.search_calls[0]["domain"] == [
        ["move_type", "=", "in_invoice"],
        ["invoice_origin", "=", "ettn-1"],
        ["company_id", "=", 9],
        ["partner_id", "=", 101],
    ]


async def test_account_move_repository_rejects_missing_company_for_vendor_bill_write() -> None:
    repository = AccountMoveRepository(client=FakeJson2Client())
    vendor_bill = VendorBill(
        supplier_id=101,
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 30),
        currency="TRY",
        external_uuid="uuid-1",
        reference="INV-1",
        company_id=None,
        invoice_lines=(
            VendorBillLine(
                product_id=501,
                quantity=Decimal("2"),
                uom="NIU",
                unit_price=Decimal("10.50"),
                tax_ids=(401,),
                description="Line 1",
            ),
        ),
    )

    with pytest.raises(VendorBillWriteValidationError):
        await repository.create_draft_vendor_bill(vendor_bill=vendor_bill, idempotency_key="ettn-1")


async def test_account_move_repository_rejects_multiple_existing_vendor_bills() -> None:
    repository = AccountMoveRepository(
        client=FakeJson2Client(
            search_records=[
                {"id": 7001, "name": "BILL/2026/001"},
                {"id": 7002, "name": "BILL/2026/002"},
            ]
        )
    )

    with pytest.raises(VendorBillWriteDuplicateError) as exc_info:
        await repository.find_existing_vendor_bill(vendor_bill=_vendor_bill(), idempotency_key="ettn-1")

    assert exc_info.value.safe_message == "Multiple existing Odoo Vendor Bills were found."


async def test_account_move_repository_rejects_missing_idempotency_key_before_odoo_call() -> None:
    client = FakeJson2Client()
    repository = AccountMoveRepository(client=client)

    with pytest.raises(VendorBillWriteValidationError):
        await repository.create_draft_vendor_bill(vendor_bill=_vendor_bill(), idempotency_key=" ")

    assert client.create_calls == []


async def test_account_move_repository_creates_draft_customer_invoice_payload() -> None:
    client = FakeJson2Client(create_result=9101)
    repository = AccountMoveRepository(client=client)

    result = await repository.create_draft_customer_invoice(
        customer_invoice=_customer_invoice(),
        idempotency_key="customer-invoice-write:1",
    )

    assert result.id == 9101
    payload = client.create_calls[0]
    assert payload["move_type"] == "out_invoice"
    assert payload["company_id"] == 7
    assert payload["partner_id"] == 701
    assert payload["invoice_origin"] == "customer-invoice-write:1"
    assert payload["invoice_line_ids"]
    assert "action_post" not in str(payload).lower()
    assert "payment" not in str(payload).lower()
    assert "reconciled" not in str(payload).lower()


async def test_account_move_repository_finds_existing_customer_invoice_by_exact_identity() -> None:
    client = FakeJson2Client(search_records=[{"id": 9101, "name": "INV/2026/001"}])
    repository = AccountMoveRepository(client=client)

    result = await repository.find_existing_customer_invoice(
        customer_invoice=_customer_invoice(),
        idempotency_key="customer-invoice-write:1",
    )

    assert result is not None
    assert result.id == 9101
    assert client.search_calls[0]["domain"] == [
        ["move_type", "=", "out_invoice"],
        ["invoice_origin", "=", "customer-invoice-write:1"],
        ["company_id", "=", 7],
        ["partner_id", "=", 701],
    ]


async def test_account_move_repository_rejects_multiple_existing_customer_invoices() -> None:
    repository = AccountMoveRepository(client=FakeJson2Client(search_records=[{"id": 9101}, {"id": 9102}]))

    with pytest.raises(CustomerInvoiceWriteDuplicateError):
        await repository.find_existing_customer_invoice(
            customer_invoice=_customer_invoice(),
            idempotency_key="customer-invoice-write:1",
        )


async def test_account_move_repository_rejects_missing_customer_invoice_idempotency_key_before_odoo_call() -> None:
    client = FakeJson2Client()
    repository = AccountMoveRepository(client=client)

    with pytest.raises(CustomerInvoiceWriteValidationError):
        await repository.create_draft_customer_invoice(customer_invoice=_customer_invoice(), idempotency_key=" ")

    assert client.create_calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectorAuthenticationError("Odoo authentication failed."), VendorBillWriteAuthenticationError),
        (ConnectorAuthorizationError("Odoo authorization failed."), VendorBillWriteAuthorizationError),
        (ConnectorValidationError("Odoo rejected the request payload."), VendorBillWriteValidationError),
        (ConnectorTimeoutError("Odoo request timed out."), VendorBillWriteTransportError),
        (ConnectorError("Odoo returned HTTP 500."), VendorBillWriteUnexpectedErpError),
        (RuntimeError("boom"), VendorBillWriteUnexpectedErpError),
    ],
)
async def test_account_move_repository_translates_connector_errors(error: Exception, expected: type[Exception]) -> None:
    repository = AccountMoveRepository(client=FakeJson2Client(create_error=error))

    with pytest.raises(expected) as exc_info:
        await repository.create_draft_vendor_bill(vendor_bill=_vendor_bill(), idempotency_key="ettn-1")

    assert exc_info.value.safe_message


class FakeJson2Client:
    def __init__(
        self,
        *,
        create_result: int = 7001,
        create_error: Exception | None = None,
        search_records: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        self.create_result = create_result
        self.create_error = create_error
        self.search_records = list(search_records or [])
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_account_move(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.search_calls.append(
            {
                "model": model,
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
            }
        )
        return list(self.search_records)


def _vendor_bill() -> VendorBill:
    return VendorBill(
        supplier_id=101,
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 30),
        currency="TRY",
        external_uuid="uuid-1",
        reference="INV-1",
        company_id=7,
        invoice_lines=(
            VendorBillLine(
                product_id=501,
                quantity=Decimal("2"),
                uom="NIU",
                unit_price=Decimal("10.50"),
                tax_ids=(401,),
                description="Line 1",
            ),
        ),
        notes=("safe note",),
    )


def _customer_invoice() -> CustomerInvoice:
    return CustomerInvoice(
        company_id=7,
        customer_id=701,
        invoice_date=date(2026, 7, 30),
        currency="TRY",
        external_uuid="uuid-1",
        reference="Recharge ETTN-1:A",
        invoice_lines=(
            CustomerInvoiceLine(
                product_id=501,
                quantity=Decimal("1"),
                unit_price=Decimal("120.00"),
                tax_ids=(401,),
                description="Recharge",
                source_allocation_key="A",
            ),
        ),
        notes=("Source invoice: ETTN-1",),
    )
