"""OdooOneOffVendorRetirementWriter (P0-PROD-08H): read-back-first idempotent archive,
gated, no unlink capability, fails closed on identity mismatch.
"""

from __future__ import annotations

import pytest

from app.application.commands.one_off_vendor_retirement import ArchiveOneOffVendorPartnerCommand
from app.application.dto.one_off_vendor_retirement import OneOffVendorArchiveWriteStatus
from app.application.exceptions.supplier_partner import (
    SupplierPartnerDataIntegrityError,
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteUnexpectedErpError,
)
from app.erp.write.odoo_one_off_vendor_retirement_writer import OdooOneOffVendorRetirementWriter
from app.erp.write.odoo_supplier_partner_writer import OdooSupplierPartnerWritePolicy, SupplierPartnerRecord

PARTNER_ID = 6001


class _FakeRepository:
    def __init__(self, *, records: list[SupplierPartnerRecord]) -> None:
        self._by_id = {r.id: r for r in records}
        self.calls: list[int] = []

    async def read_partner(self, partner_id: int) -> SupplierPartnerRecord:
        self.calls.append(partner_id)
        record = self._by_id.get(partner_id)
        if record is None:
            raise SupplierPartnerDataIntegrityError("not found")
        return record


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.side_effect: Exception | None = None

    async def archive_res_partner(self, *, partner_id: int) -> bool:
        self.calls.append(partner_id)
        if self.side_effect is not None:
            raise self.side_effect
        return True


def _record(*, active: bool) -> SupplierPartnerRecord:
    return SupplierPartnerRecord(id=PARTNER_ID, name="D-Market", vat="2650179910", active=active, company_id=None)


def _enabled_policy() -> OdooSupplierPartnerWritePolicy:
    return OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="development",
        odoo_host="test-ictteknoloji.odoo.com",
    )


def _disabled_policy() -> OdooSupplierPartnerWritePolicy:
    return OdooSupplierPartnerWritePolicy()


async def test_already_inactive_is_idempotent_no_op() -> None:
    repository = _FakeRepository(records=[_record(active=False)])
    client = _FakeClient()
    writer = OdooOneOffVendorRetirementWriter(repository=repository, client=client, policy=_disabled_policy())

    result = await writer.archive_partner(
        ArchiveOneOffVendorPartnerCommand(partner_id=PARTNER_ID, approved_by="finance.operator")
    )

    assert result.status is OneOffVendorArchiveWriteStatus.ALREADY_ARCHIVED
    assert client.calls == []  # no write attempted -- gate never even consulted
    assert repository.calls == [PARTNER_ID]


async def test_active_partner_archived_when_gate_enabled() -> None:
    repository = _FakeRepository(records=[_record(active=True), _record(active=False)])
    # First read-back (before write) sees active=True; the second (after write) must
    # see active=False to confirm -- simulate via a repository that flips after write.
    calls = {"n": 0}

    class _FlippingRepository(_FakeRepository):
        async def read_partner(self, partner_id: int) -> SupplierPartnerRecord:
            calls["n"] += 1
            self.calls.append(partner_id)
            return _record(active=calls["n"] == 1)

    repository = _FlippingRepository(records=[])
    client = _FakeClient()
    writer = OdooOneOffVendorRetirementWriter(repository=repository, client=client, policy=_enabled_policy())

    result = await writer.archive_partner(
        ArchiveOneOffVendorPartnerCommand(partner_id=PARTNER_ID, approved_by="finance.operator")
    )

    assert result.status is OneOffVendorArchiveWriteStatus.ARCHIVED
    assert client.calls == [PARTNER_ID]
    assert repository.calls == [PARTNER_ID, PARTNER_ID]  # read-back before AND after


async def test_write_gate_disabled_fails_closed_before_any_write() -> None:
    repository = _FakeRepository(records=[_record(active=True)])
    client = _FakeClient()
    writer = OdooOneOffVendorRetirementWriter(repository=repository, client=client, policy=_disabled_policy())

    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await writer.archive_partner(
            ArchiveOneOffVendorPartnerCommand(partner_id=PARTNER_ID, approved_by="finance.operator")
        )
    assert client.calls == []


async def test_missing_partner_fails_closed() -> None:
    repository = _FakeRepository(records=[])
    client = _FakeClient()
    writer = OdooOneOffVendorRetirementWriter(repository=repository, client=client, policy=_enabled_policy())

    with pytest.raises(SupplierPartnerDataIntegrityError):
        await writer.archive_partner(
            ArchiveOneOffVendorPartnerCommand(partner_id=PARTNER_ID, approved_by="finance.operator")
        )
    assert client.calls == []


async def test_write_still_active_after_reported_success_fails_closed() -> None:
    """The write reports success but the confirming read-back still shows active --
    never claim ARCHIVED on an unconfirmed outcome."""

    class _StubbornRepository(_FakeRepository):
        async def read_partner(self, partner_id: int) -> SupplierPartnerRecord:
            self.calls.append(partner_id)
            return _record(active=True)

    repository = _StubbornRepository(records=[])
    client = _FakeClient()
    writer = OdooOneOffVendorRetirementWriter(repository=repository, client=client, policy=_enabled_policy())

    with pytest.raises(SupplierPartnerDataIntegrityError):
        await writer.archive_partner(
            ArchiveOneOffVendorPartnerCommand(partner_id=PARTNER_ID, approved_by="finance.operator")
        )
    assert client.calls == [PARTNER_ID]


async def test_transport_failure_propagates_as_uncertain() -> None:
    class _FlippingRepository(_FakeRepository):
        async def read_partner(self, partner_id: int) -> SupplierPartnerRecord:
            self.calls.append(partner_id)
            return _record(active=True)

    repository = _FlippingRepository(records=[])
    client = _FakeClient()
    client.side_effect = TimeoutError("boom")
    writer = OdooOneOffVendorRetirementWriter(repository=repository, client=client, policy=_enabled_policy())

    with pytest.raises(SupplierPartnerWriteUnexpectedErpError):
        await writer.archive_partner(
            ArchiveOneOffVendorPartnerCommand(partner_id=PARTNER_ID, approved_by="finance.operator")
        )


def test_no_unlink_capability_anywhere_in_writer_source() -> None:
    from pathlib import Path

    source = Path("app/erp/write/odoo_one_off_vendor_retirement_writer.py").read_text(encoding="utf-8")
    assert "delete" not in source.lower()
    assert "unlink" not in source.lower()
    assert "create_res_partner" not in source
