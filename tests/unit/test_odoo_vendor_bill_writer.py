from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.application.commands import VendorBillWriteCommand
from app.billing import VendorBill, VendorBillLine
from app.erp.write import (
    AccountMoveDraft,
    OdooVendorBillWritePolicy,
    OdooVendorBillWriter,
    VendorBillWriteSafetyGateError,
    VendorBillWriteValidationError,
)


async def test_writer_returns_dry_run_without_repository_calls() -> None:
    repository = FakeDraftVendorBillRepository()
    writer = OdooVendorBillWriter(repository=repository, policy=_enabled_policy())

    result = await writer.write_vendor_bill(
        VendorBillWriteCommand(vendor_bill=_vendor_bill(), idempotency_key="ettn-1", dry_run=True)
    )

    assert result.status == "dry_run"
    assert result.success is True
    assert result.idempotency_key == "ettn-1"
    assert result.external_id is None
    assert result.vendor_bill_id is None
    assert result.warnings == ("Dry run completed. No Odoo Vendor Bill was created.",)
    assert repository.find_calls == []
    assert repository.create_calls == []


async def test_writer_creates_draft_vendor_bill_after_duplicate_check() -> None:
    repository = FakeDraftVendorBillRepository(created=AccountMoveDraft(id=7001))
    writer = OdooVendorBillWriter(repository=repository, policy=_enabled_policy())

    result = await writer.write_vendor_bill(
        VendorBillWriteCommand(
            vendor_bill=_vendor_bill(),
            idempotency_key=" ettn-1 ",
            dry_run=False,
            approved_by="finance-user",
        )
    )

    assert result.status == "created"
    assert result.success is True
    assert result.external_id == 7001
    assert result.vendor_bill_id == 7001
    assert result.external_model == "account.move"
    assert result.idempotency_key == "ettn-1"
    assert repository.find_calls == [("ettn-1", 101)]
    assert repository.create_calls == [("ettn-1", 101)]


async def test_writer_returns_existing_without_second_create() -> None:
    repository = FakeDraftVendorBillRepository(existing=AccountMoveDraft(id=7001, name="BILL/2026/001"))
    writer = OdooVendorBillWriter(repository=repository, policy=_enabled_policy())

    result = await writer.write_vendor_bill(
        VendorBillWriteCommand(
            vendor_bill=_vendor_bill(),
            idempotency_key="ettn-1",
            dry_run=False,
            approved_by="finance-user",
        )
    )

    assert result.status == "existing"
    assert result.success is True
    assert result.already_exists is True
    assert result.external_id == 7001
    assert result.vendor_bill_id == 7001
    assert result.draft_number == "BILL/2026/001"
    assert result.external_model == "account.move"
    assert repository.find_calls == [("ettn-1", 101)]
    assert repository.create_calls == []


@pytest.mark.parametrize(
    "policy",
    [
        OdooVendorBillWritePolicy(
            production_operations_enabled=False,
            production_approval_ack="APPROVED_FOR_PRODUCTION",
        ),
        OdooVendorBillWritePolicy(production_operations_enabled=True, production_approval_ack=""),
    ],
)
async def test_writer_blocks_real_write_when_safety_gate_is_missing(policy: OdooVendorBillWritePolicy) -> None:
    repository = FakeDraftVendorBillRepository()
    writer = OdooVendorBillWriter(repository=repository, policy=policy)

    with pytest.raises(VendorBillWriteSafetyGateError):
        await writer.write_vendor_bill(
            VendorBillWriteCommand(
                vendor_bill=_vendor_bill(),
                idempotency_key="ettn-1",
                dry_run=False,
                approved_by="finance-user",
            )
        )

    assert repository.find_calls == []
    assert repository.create_calls == []


async def test_writer_blocks_real_write_without_named_approver() -> None:
    writer = OdooVendorBillWriter(repository=FakeDraftVendorBillRepository(), policy=_enabled_policy())

    with pytest.raises(VendorBillWriteSafetyGateError):
        await writer.write_vendor_bill(
            VendorBillWriteCommand(vendor_bill=_vendor_bill(), idempotency_key="ettn-1", dry_run=False)
        )


async def test_writer_rejects_missing_idempotency_key() -> None:
    writer = OdooVendorBillWriter(repository=FakeDraftVendorBillRepository(), policy=_enabled_policy())

    with pytest.raises(VendorBillWriteValidationError):
        await writer.write_vendor_bill(
            VendorBillWriteCommand(
                vendor_bill=_vendor_bill(),
                idempotency_key=" ",
                dry_run=False,
                approved_by="finance-user",
            )
        )


class FakeDraftVendorBillRepository:
    def __init__(
        self,
        *,
        existing: AccountMoveDraft | None = None,
        created: AccountMoveDraft | None = None,
    ) -> None:
        self.existing = existing
        self.created = created or AccountMoveDraft(id=7001)
        self.find_calls: list[tuple[str, int]] = []
        self.create_calls: list[tuple[str, int]] = []

    async def find_existing_vendor_bill(
        self,
        *,
        vendor_bill: VendorBill,
        idempotency_key: str,
    ) -> AccountMoveDraft | None:
        self.find_calls.append((idempotency_key, vendor_bill.supplier_id))
        return self.existing

    async def create_draft_vendor_bill(
        self,
        *,
        vendor_bill: VendorBill,
        idempotency_key: str,
    ) -> AccountMoveDraft:
        self.create_calls.append((idempotency_key, vendor_bill.supplier_id))
        return self.created


def _enabled_policy() -> OdooVendorBillWritePolicy:
    return OdooVendorBillWritePolicy(
        production_operations_enabled=True,
        production_approval_ack="APPROVED_FOR_PRODUCTION",
    )


def _vendor_bill() -> VendorBill:
    return VendorBill(
        supplier_id=101,
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 30),
        currency="TRY",
        external_uuid="uuid-1",
        reference="INV-1",
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
