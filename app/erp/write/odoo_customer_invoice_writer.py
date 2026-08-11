from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.commands import CustomerInvoiceWriteCommand
from app.application.dto import CustomerInvoiceWriteResult
from app.application.ports import CustomerInvoiceWriter
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write.account_move_repository import AccountMoveDraft
from app.erp.write.exceptions import CustomerInvoiceWriteSafetyGateError, CustomerInvoiceWriteValidationError


class DraftCustomerInvoiceRepository(Protocol):
    async def find_existing_customer_invoice(
        self,
        *,
        customer_invoice: object,
        idempotency_key: str,
    ) -> AccountMoveDraft | None:
        pass

    async def create_draft_customer_invoice(
        self,
        *,
        customer_invoice: object,
        idempotency_key: str,
    ) -> AccountMoveDraft:
        pass


@dataclass(frozen=True, slots=True)
class OdooCustomerInvoiceWritePolicy:
    production_operations_enabled: bool = False
    production_approval_ack: str = ""
    customer_invoice_execute_enabled: bool = False
    required_approval_ack: str = PRODUCTION_APPROVAL_ACK

    @classmethod
    def from_settings(cls, settings: Settings) -> OdooCustomerInvoiceWritePolicy:
        return cls(
            production_operations_enabled=settings.production_operations_enabled,
            production_approval_ack=settings.production_approval_ack,
            customer_invoice_execute_enabled=settings.customer_invoice_execute_enabled,
        )

    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        if not self.production_operations_enabled:
            raise CustomerInvoiceWriteSafetyGateError("Production operations must be explicitly enabled.")
        if self.production_approval_ack != self.required_approval_ack:
            raise CustomerInvoiceWriteSafetyGateError("Production approval acknowledgement is required.")
        if not self.customer_invoice_execute_enabled:
            raise CustomerInvoiceWriteSafetyGateError("Customer Invoice execution must be explicitly enabled.")
        if approved_by is None or not approved_by.strip():
            raise CustomerInvoiceWriteSafetyGateError("A named approver is required for Customer Invoice creation.")


class OdooCustomerInvoiceWriter(CustomerInvoiceWriter):
    """CustomerInvoiceWriter implementation for draft-only Odoo account.move creation."""

    def __init__(self, *, repository: DraftCustomerInvoiceRepository, policy: OdooCustomerInvoiceWritePolicy) -> None:
        self._repository = repository
        self._policy = policy

    async def write_customer_invoice(self, command: CustomerInvoiceWriteCommand) -> CustomerInvoiceWriteResult:
        idempotency_key = _idempotency_key(command)
        if command.dry_run:
            return CustomerInvoiceWriteResult(
                status="dry_run",
                idempotency_key=idempotency_key,
                safe_message="Dry run completed. No Odoo Customer Invoice was created.",
                success=True,
                warnings=("Dry run completed. No Odoo Customer Invoice was created.",),
            )

        self._policy.ensure_real_write_allowed(approved_by=command.approved_by)
        existing = await self._repository.find_existing_customer_invoice(
            customer_invoice=command.customer_invoice,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return _existing_result(idempotency_key=idempotency_key, existing=existing)

        created = await self._repository.create_draft_customer_invoice(
            customer_invoice=command.customer_invoice,
            idempotency_key=idempotency_key,
        )
        return CustomerInvoiceWriteResult(
            status="created",
            idempotency_key=idempotency_key,
            external_id=created.id,
            external_model="account.move",
            safe_message="Draft Customer Invoice created in Odoo.",
            success=True,
            customer_invoice_id=created.id,
            draft_number=created.name,
        )


def _idempotency_key(command: CustomerInvoiceWriteCommand) -> str:
    idempotency_key = command.idempotency_key.strip()
    if not idempotency_key:
        raise CustomerInvoiceWriteValidationError("Customer Invoice idempotency key is required.")
    return idempotency_key


def _existing_result(*, idempotency_key: str, existing: AccountMoveDraft) -> CustomerInvoiceWriteResult:
    return CustomerInvoiceWriteResult(
        status="existing",
        idempotency_key=idempotency_key,
        external_id=existing.id,
        external_model="account.move",
        safe_message="Draft Customer Invoice already exists in Odoo.",
        success=True,
        customer_invoice_id=existing.id,
        draft_number=existing.name,
        already_exists=True,
        warnings=("Draft Customer Invoice already exists in Odoo.",),
    )
