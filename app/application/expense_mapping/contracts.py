from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.dto import ApplicationDTO
from app.application.expense_mapping.exceptions import OperatingExpenseMappingContractError

EXPENSE_CATEGORY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class OperatingExpenseMapping(ApplicationDTO):
    """Immutable deterministic mapping from a resolved supplier to an expense account.

    Identity is ``(company_id, vendor_partner_id)``. ``vendor_partner_id`` is an
    Odoo ``res.partner`` id already resolved deterministically by the Hub;
    ``expense_account_id`` is an Odoo ``account.account`` id that later PRs pin into
    immutable Vendor Bill evidence. ``expense_category`` is a short stable code, not
    free-form classification prose. There are no ERP foreign keys: the integer ids
    are external identifiers.
    """

    id: int
    company_id: int
    vendor_partner_id: int
    expense_account_id: int
    expense_category: str
    enabled: bool

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "id must be a positive integer.")
        _require_positive_int(self.company_id, "company_id must be a positive integer.")
        _require_positive_int(self.vendor_partner_id, "vendor_partner_id must be a positive integer.")
        _require_positive_int(self.expense_account_id, "expense_account_id must be a positive integer.")
        if not isinstance(self.expense_category, str) or not EXPENSE_CATEGORY_PATTERN.match(self.expense_category):
            raise OperatingExpenseMappingContractError(
                "expense_category must be a short uppercase code matching ^[A-Z][A-Z0-9_]{0,63}$."
            )
        if type(self.enabled) is not bool:
            raise OperatingExpenseMappingContractError("enabled must be a boolean.")


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise OperatingExpenseMappingContractError(message)
