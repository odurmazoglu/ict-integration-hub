"""Explicit human-selected expense account for one account-only invoice line (P0-PROD-08G).

An operator's explicit ``LineResolution.expense_account_id`` is validated here --
read-only, before the decision is accepted -- against a minimal Odoo
``account.account`` snapshot. Unlike ``selected_product_resolution``, there is
nothing to substitute into pinned evidence: the operator's own account id is
exactly what gets persisted (as part of ``LineResolution`` itself, round-tripped
through the existing decision JSON) and later read by ``VendorBillBuilder`` --
this module only proves it is real and scoped to the right company before that
happens, and never replaces it with another value.

Never inferred from ``PRODUCT_NOT_FOUND`` or from the supplier -- only applied
for a line explicitly carrying ``LineResolution.expense_account_id``. Never
written into ``OperatingExpenseMappingRecord``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ApplicationDTO
from app.application.workbench.dto import LineResolution
from app.application.workbench.exceptions import ReviewDecisionError


@dataclass(frozen=True, slots=True)
class ResolutionAccountRecord(ApplicationDTO):
    """Minimal read-only projection of an Odoo ``account.account`` for selection validation."""

    id: int
    company_ids: tuple[int, ...]


def selected_expense_account_ids(line_resolutions: tuple[LineResolution, ...]) -> tuple[int, ...]:
    """Distinct positive expense-account ids explicitly selected across ``line_resolutions``."""

    ids: list[int] = []
    seen: set[int] = set()
    for resolution in line_resolutions:
        account_id = resolution.expense_account_id
        if account_id is not None and account_id not in seen:
            seen.add(account_id)
            ids.append(account_id)
    return tuple(ids)


def validate_selected_expense_accounts(
    *,
    line_resolutions: tuple[LineResolution, ...],
    company_id: int,
    accounts_by_id: dict[int, ResolutionAccountRecord],
) -> None:
    """Fail closed the moment any explicitly selected expense account does not exist or
    is not scoped to ``company_id``.

    The whole decision submission aborts rather than silently accepting one line's
    account and dropping another's -- mirrors ``apply_selected_product_resolutions``'s
    fail-closed discipline exactly.
    """

    for resolution in line_resolutions:
        account_id = resolution.expense_account_id
        if account_id is None:
            continue
        _validated_account_record(accounts_by_id.get(account_id), account_id=account_id, company_id=company_id)


def _validated_account_record(
    record: ResolutionAccountRecord | None,
    *,
    account_id: int,
    company_id: int,
) -> ResolutionAccountRecord:
    if record is None:
        raise ReviewDecisionError("The selected expense account does not exist.")
    if type(record.id) is not int or record.id != account_id:
        raise ReviewDecisionError("The selected expense account id is invalid.")
    if company_id not in record.company_ids:
        raise ReviewDecisionError("The selected expense account is scoped to a different company.")
    return record
