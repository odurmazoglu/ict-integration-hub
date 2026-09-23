"""Read-only operator surface for selecting a valid Odoo operating-expense account (P0-PROD-15P).

Exists to close the gap identified in P0-PROD-15O: ``OnboardOperatingExpenseMappingUseCase``
requires an explicit, human-selected ``expense_account_id``, but there was no supported way
for an operator to discover one. This module is deliberately narrow: it is not a generic
Odoo ``account.account`` browser. The server, never the caller, controls the target model,
the base domain (company scope + eligible account type), and the field list; the caller may
supply only an optional, narrowly-validated free-text query to filter by code/name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError

#: Bounds the optional free-text filter -- not a security boundary (the Odoo JSON-2 API
#: takes a structured domain, never raw SQL/query text), just sane input hygiene.
EXPENSE_ACCOUNT_QUERY_PATTERN = re.compile(r"^[^\x00-\x1f]{1,64}$")


@dataclass(frozen=True, slots=True)
class ExpenseAccountCandidate(ApplicationDTO):
    """Minimal read-only projection of an Odoo ``account.account`` for operator selection.

    Deliberately carries only what an operator needs to recognize and pick the right
    account -- never a generic passthrough of arbitrary Odoo fields.
    """

    id: int
    code: str
    name: str
    account_type: str

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "id must be a positive integer.")
        _require_text(self.code, "code is required.")
        _require_text(self.name, "name is required.")
        _require_text(self.account_type, "account_type is required.")


@dataclass(frozen=True, slots=True)
class ListExpenseAccountCandidatesQuery(ApplicationDTO):
    """One authenticated operator's request to list eligible expense accounts.

    ``company_id`` always comes from the trusted ``RequestContext`` -- never the caller.
    """

    company_id: int
    query: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be positive.")
        if self.query is not None:
            normalized = self.query.strip()
            if not normalized or not EXPENSE_ACCOUNT_QUERY_PATTERN.match(normalized):
                raise WorkbenchContractError("query must be 1-64 characters with no control characters.")
            object.__setattr__(self, "query", normalized)


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_text(value: object, message: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


__all__ = [
    "ExpenseAccountCandidate",
    "ListExpenseAccountCandidatesQuery",
]
