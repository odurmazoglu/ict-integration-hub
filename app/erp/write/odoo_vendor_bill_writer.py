from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.commands import VendorBillWriteCommand
from app.application.dto import VendorBillWriteResult
from app.application.ports import VendorBillWriter
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write.account_move_repository import AccountMoveDraft
from app.erp.write.exceptions import (
    VendorBillWriteSafetyGateError,
    VendorBillWriteValidationError,
)


class DraftVendorBillRepository(Protocol):
    async def find_existing_vendor_bill(
        self,
        *,
        vendor_bill: object,
        idempotency_key: str,
        company_id: int | None = None,
    ) -> AccountMoveDraft | None:
        pass

    async def create_draft_vendor_bill(
        self,
        *,
        vendor_bill: object,
        idempotency_key: str,
        company_id: int | None = None,
    ) -> AccountMoveDraft:
        pass


@dataclass(frozen=True, slots=True)
class OdooVendorBillWritePolicy:
    production_operations_enabled: bool = False
    production_approval_ack: str = ""
    required_approval_ack: str = PRODUCTION_APPROVAL_ACK

    @classmethod
    def from_settings(cls, settings: Settings) -> OdooVendorBillWritePolicy:
        return cls(
            production_operations_enabled=settings.production_operations_enabled,
            production_approval_ack=settings.production_approval_ack,
        )

    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        if not self.production_operations_enabled:
            raise VendorBillWriteSafetyGateError("Production operations must be explicitly enabled.")
        if self.production_approval_ack != self.required_approval_ack:
            raise VendorBillWriteSafetyGateError("Production approval acknowledgement is required.")
        if approved_by is None or not approved_by.strip():
            raise VendorBillWriteSafetyGateError("A named approver is required for Vendor Bill creation.")


class OdooVendorBillWriter(VendorBillWriter):
    """VendorBillWriter implementation for draft-only Odoo account.move creation."""

    def __init__(self, *, repository: DraftVendorBillRepository, policy: OdooVendorBillWritePolicy) -> None:
        self._repository = repository
        self._policy = policy

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        idempotency_key = _idempotency_key(command)
        if command.dry_run:
            return VendorBillWriteResult(
                status="dry_run",
                idempotency_key=idempotency_key,
                safe_message="Dry run completed. No Odoo Vendor Bill was created.",
                success=True,
                warnings=("Dry run completed. No Odoo Vendor Bill was created.",),
            )

        self._policy.ensure_real_write_allowed(approved_by=command.approved_by)
        company_id = _company_id(command)
        existing = await self._repository.find_existing_vendor_bill(
            vendor_bill=command.vendor_bill,
            idempotency_key=idempotency_key,
            company_id=company_id,
        )
        if existing is not None:
            return _existing_result(idempotency_key=idempotency_key, existing=existing)

        created = await self._repository.create_draft_vendor_bill(
            vendor_bill=command.vendor_bill,
            idempotency_key=idempotency_key,
            company_id=company_id,
        )
        return VendorBillWriteResult(
            status="created",
            idempotency_key=idempotency_key,
            external_id=created.id,
            external_model="account.move",
            safe_message="Draft Vendor Bill created in Odoo.",
            success=True,
            vendor_bill_id=created.id,
            draft_number=created.name,
        )


def _idempotency_key(command: VendorBillWriteCommand) -> str:
    idempotency_key = command.idempotency_key.strip()
    if not idempotency_key:
        raise VendorBillWriteValidationError("Vendor Bill idempotency key is required.")
    return idempotency_key


def _company_id(command: VendorBillWriteCommand) -> int:
    candidate = command.company_id
    if candidate is None:
        candidate = command.vendor_bill.company_id
    if type(candidate) is not int or candidate <= 0:
        raise VendorBillWriteValidationError("Vendor Bill company_id is required for Odoo write operations.")
    return candidate


def _existing_result(*, idempotency_key: str, existing: AccountMoveDraft) -> VendorBillWriteResult:
    return VendorBillWriteResult(
        status="existing",
        idempotency_key=idempotency_key,
        external_id=existing.id,
        external_model="account.move",
        safe_message="Draft Vendor Bill already exists in Odoo.",
        success=True,
        vendor_bill_id=existing.id,
        draft_number=existing.name,
        already_exists=True,
        warnings=("Draft Vendor Bill already exists in Odoo.",),
    )
