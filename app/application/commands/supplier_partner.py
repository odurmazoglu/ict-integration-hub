from __future__ import annotations

from dataclasses import dataclass

from app.application.commands.base import Command
from app.application.exceptions.supplier_partner import SupplierPartnerWriteValidationError


@dataclass(frozen=True, slots=True)
class CreateSupplierPartnerCommand(Command):
    """Application request to create one Odoo supplier ``res.partner`` from immutable identity.

    Only legal identity is carried: the supplier's legal name and tax number
    (VKN/TCKN). No email, phone, address, payment terms, bank details, accounting
    properties, or tags -- none of those are safe to infer from an invoice.

    ``supplier_name`` / ``supplier_tax_number`` reach this command from the
    application layer. The future P0-3D2D remediation flow MUST source them from
    the immutable ``ReviewSourceInvoiceEvidence`` for the review, never from HTTP
    client input.
    """

    company_id: int
    supplier_name: str
    supplier_tax_number: str
    idempotency_key: str
    approved_by: str | None = None

    def __post_init__(self) -> None:
        if type(self.company_id) is not int or self.company_id <= 0:
            raise SupplierPartnerWriteValidationError("A positive company_id is required.")
        if not isinstance(self.supplier_name, str) or not self.supplier_name.strip():
            raise SupplierPartnerWriteValidationError("supplier_name is required.")
        if not isinstance(self.supplier_tax_number, str) or not self.supplier_tax_number.strip():
            raise SupplierPartnerWriteValidationError("supplier_tax_number is required.")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise SupplierPartnerWriteValidationError("idempotency_key is required.")
        if self.approved_by is not None and (not isinstance(self.approved_by, str) or not self.approved_by.strip()):
            raise SupplierPartnerWriteValidationError("approved_by must be a non-empty name when provided.")
