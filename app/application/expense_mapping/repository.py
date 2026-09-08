from __future__ import annotations

from typing import Protocol

from app.application.expense_mapping.contracts import OperatingExpenseMapping


class OperatingExpenseMappingRepository(Protocol):
    """Read-only port for the deterministic supplier -> expense-account mapping.

    Partner resolution stays the responsibility of ``PartnerMatchingEngine``; this
    port is keyed only by already-resolved deterministic identifiers.
    """

    def find_for_supplier(
        self,
        *,
        company_id: int,
        vendor_partner_id: int,
    ) -> OperatingExpenseMapping | None:
        """Return the single enabled mapping for the supplier, or ``None``.

        Fails closed with
        :class:`app.application.expense_mapping.exceptions.OperatingExpenseMappingDataIntegrityError`
        if persistence is somehow exposed to more than one enabled mapping for the
        same ``(company_id, vendor_partner_id)``.
        """
