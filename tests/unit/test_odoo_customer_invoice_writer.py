from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.application.commands import CustomerInvoiceWriteCommand
from app.billing import CustomerInvoice, CustomerInvoiceLine
from app.erp.write import (
    AccountMoveDraft,
    CustomerInvoiceWriteSafetyGateError,
    CustomerInvoiceWriteValidationError,
    OdooCustomerInvoiceWritePolicy,
    OdooCustomerInvoiceWriter,
)


async def test_customer_invoice_writer_returns_dry_run_without_repository_calls() -> None:
    repository = FakeDraftCustomerInvoiceRepository()
    writer = OdooCustomerInvoiceWriter(repository=repository, policy=_enabled_policy())

    result = await writer.write_customer_invoice(
        CustomerInvoiceWriteCommand(
            customer_invoice=_customer_invoice(),
            idempotency_key="customer-invoice-write:1",
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.success is True
    assert repository.find_calls == []
    assert repository.create_calls == []


async def test_customer_invoice_writer_creates_after_duplicate_check() -> None:
    repository = FakeDraftCustomerInvoiceRepository(created=AccountMoveDraft(id=9101))
    writer = OdooCustomerInvoiceWriter(repository=repository, policy=_enabled_policy())

    result = await writer.write_customer_invoice(
        CustomerInvoiceWriteCommand(
            customer_invoice=_customer_invoice(),
            idempotency_key=" customer-invoice-write:1 ",
            dry_run=False,
            approved_by="finance-user",
        )
    )

    assert result.status == "created"
    assert result.external_id == 9101
    assert result.customer_invoice_id == 9101
    assert result.external_model == "account.move"
    assert result.idempotency_key == "customer-invoice-write:1"
    assert repository.find_calls == [("customer-invoice-write:1", 7, 701)]
    assert repository.create_calls == [("customer-invoice-write:1", 7, 701)]


async def test_customer_invoice_writer_returns_existing_without_second_create() -> None:
    repository = FakeDraftCustomerInvoiceRepository(existing=AccountMoveDraft(id=9101, name="INV/2026/001"))
    writer = OdooCustomerInvoiceWriter(repository=repository, policy=_enabled_policy())

    result = await writer.write_customer_invoice(
        CustomerInvoiceWriteCommand(
            customer_invoice=_customer_invoice(),
            idempotency_key="customer-invoice-write:1",
            dry_run=False,
            approved_by="finance-user",
        )
    )

    assert result.status == "existing"
    assert result.already_exists is True
    assert result.external_id == 9101
    assert result.customer_invoice_id == 9101
    assert result.draft_number == "INV/2026/001"
    assert repository.create_calls == []


@pytest.mark.parametrize(
    "policy",
    [
        OdooCustomerInvoiceWritePolicy(
            production_operations_enabled=False,
            production_approval_ack="APPROVED_FOR_PRODUCTION",
            customer_invoice_execute_enabled=True,
        ),
        OdooCustomerInvoiceWritePolicy(
            production_operations_enabled=True,
            production_approval_ack="",
            customer_invoice_execute_enabled=True,
        ),
        OdooCustomerInvoiceWritePolicy(
            production_operations_enabled=True,
            production_approval_ack="APPROVED_FOR_PRODUCTION",
            customer_invoice_execute_enabled=False,
        ),
    ],
)
async def test_customer_invoice_writer_blocks_real_write_when_safety_gate_is_missing(
    policy: OdooCustomerInvoiceWritePolicy,
) -> None:
    repository = FakeDraftCustomerInvoiceRepository()
    writer = OdooCustomerInvoiceWriter(repository=repository, policy=policy)

    with pytest.raises(CustomerInvoiceWriteSafetyGateError):
        await writer.write_customer_invoice(
            CustomerInvoiceWriteCommand(
                customer_invoice=_customer_invoice(),
                idempotency_key="customer-invoice-write:1",
                dry_run=False,
                approved_by="finance-user",
            )
        )

    assert repository.find_calls == []
    assert repository.create_calls == []


async def test_customer_invoice_writer_blocks_real_write_without_named_approver() -> None:
    writer = OdooCustomerInvoiceWriter(repository=FakeDraftCustomerInvoiceRepository(), policy=_enabled_policy())

    with pytest.raises(CustomerInvoiceWriteSafetyGateError):
        await writer.write_customer_invoice(
            CustomerInvoiceWriteCommand(
                customer_invoice=_customer_invoice(),
                idempotency_key="customer-invoice-write:1",
                dry_run=False,
            )
        )


async def test_customer_invoice_writer_rejects_missing_idempotency_key() -> None:
    writer = OdooCustomerInvoiceWriter(repository=FakeDraftCustomerInvoiceRepository(), policy=_enabled_policy())

    with pytest.raises(CustomerInvoiceWriteValidationError):
        await writer.write_customer_invoice(
            CustomerInvoiceWriteCommand(
                customer_invoice=_customer_invoice(),
                idempotency_key=" ",
                dry_run=False,
                approved_by="finance-user",
            )
        )


class FakeDraftCustomerInvoiceRepository:
    def __init__(
        self,
        *,
        existing: AccountMoveDraft | None = None,
        created: AccountMoveDraft | None = None,
    ) -> None:
        self.existing = existing
        self.created = created or AccountMoveDraft(id=9101)
        self.find_calls: list[tuple[str, int, int]] = []
        self.create_calls: list[tuple[str, int, int]] = []

    async def find_existing_customer_invoice(
        self,
        *,
        customer_invoice: CustomerInvoice,
        idempotency_key: str,
    ) -> AccountMoveDraft | None:
        self.find_calls.append((idempotency_key, customer_invoice.company_id, customer_invoice.customer_id))
        return self.existing

    async def create_draft_customer_invoice(
        self,
        *,
        customer_invoice: CustomerInvoice,
        idempotency_key: str,
    ) -> AccountMoveDraft:
        self.create_calls.append((idempotency_key, customer_invoice.company_id, customer_invoice.customer_id))
        return self.created


def _enabled_policy() -> OdooCustomerInvoiceWritePolicy:
    return OdooCustomerInvoiceWritePolicy(
        production_operations_enabled=True,
        production_approval_ack="APPROVED_FOR_PRODUCTION",
        customer_invoice_execute_enabled=True,
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
    )
