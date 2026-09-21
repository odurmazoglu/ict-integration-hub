from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.application.commands.base import Command
from app.application.exceptions.supplier_partner import SupplierPartnerWriteValidationError

if TYPE_CHECKING:
    # Deferred: app.application.workbench's package __init__ eagerly imports use
    # cases that themselves import this module, so a real-time import here would
    # cycle. Safe because of `from __future__ import annotations` above -- the
    # WriteAuthorizationRecord annotation is never evaluated at runtime.
    from app.application.workbench.write_authorization import WriteAuthorizationRecord


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

    ``authorize_inactive_reuse`` (P0-PROD-09C) is an optional, caller-supplied
    predicate consulted ONLY when the exact-VAT lookup finds exactly one existing
    but *inactive* (archived) partner. The writer itself carries no notion of "Hub
    ownership" -- that is application/domain knowledge -- so by default (``None``,
    every caller except ONE_OFF_VENDOR reuse) an inactive exact-VAT match still
    fails closed exactly as before. Only ``ResolveWorkbenchSupplierUseCase``'s
    ONE_OFF_VENDOR path supplies a predicate, and only after checking its own
    ``SupplierRemediationEffect`` ownership ledger -- the writer never infers
    ownership itself, it only asks the question the caller hands it.

    ``authorization`` (P0-PROD-09F) is an optional, already-claimed-and-consumed
    ``WriteAuthorizationRecord`` for this exact write. When present, it lets
    ``OdooSupplierPartnerWritePolicy.ensure_real_write_allowed`` bypass ONLY
    ``supplier_remediation_write_enabled`` -- the master production kill switch,
    approval acknowledgement, and named-approver check are never bypassed. Consumed
    strictly before this command is ever constructed (see
    ``ResolveWorkbenchSupplierUseCase``); this field never itself performs a claim.
    """

    company_id: int
    supplier_name: str
    supplier_tax_number: str
    idempotency_key: str
    approved_by: str | None = None
    authorize_inactive_reuse: Callable[[int], bool] | None = None
    authorization: WriteAuthorizationRecord | None = None

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
