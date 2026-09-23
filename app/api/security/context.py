from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from app.api.security.exceptions import InvalidAuthenticationContextError


class AuthenticationMethod(StrEnum):
    """Canonical authentication methods recognized by the API boundary."""

    DEVELOPMENT_HEADERS = "development_headers"
    JWT = "jwt"
    OAUTH2 = "oauth2"
    AZURE_AD = "azure_ad"
    ODOO_SESSION = "odoo_session"
    SERVICE_ACCOUNT = "service_account"


class Permission(StrEnum):
    """Canonical API permission claims carried by RequestContext."""

    WORKBENCH_REVIEW_READ = "workbench_review_read"
    WORKBENCH_REVIEW_DECIDE = "workbench_review_decide"
    WORKBENCH_EXECUTE = "workbench_execute"
    #: P0-PROD-12B. Authorizes triggering a controlled Uyumsoft invoice sync/ingestion
    #: run (POST /api/v1/sync/uyumsoft/invoices). Distinct from every Workbench
    #: permission: Uyumsoft sync has no review_id/company_id-scoped concept of its
    #: own (see UYUMSOFT_SYNC_EXECUTE_ENABLED and the router's own gate checks,
    #: which remain the separate "is this operation currently enabled" control).
    UYUMSOFT_SYNC_EXECUTE = "uyumsoft_sync_execute"
    #: P0-PROD-12B. Authorizes reading/fetching source invoice documents from the
    #: Uyumsoft provider (POST /api/v1/documents/uyumsoft/invoices/download).
    #: Distinct from every Workbench permission -- this is provider-document
    #: access, not a Workbench review action.
    INVOICE_DOCUMENT_READ = "invoice_document_read"
    #: P0-PROD-15P. Authorizes the read-only Odoo account.account lookup
    #: (GET /api/workbench/expense-accounts) used to select a valid operating-expense
    #: account. Distinct from WORKBENCH_REVIEW_READ: it is chart-of-accounts access, not
    #: a review read, and grants no review-mutation capability.
    WORKBENCH_EXPENSE_ACCOUNT_READ = "workbench_expense_account_read"


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Trusted API request context consumed by future route adapters."""

    user_id: str
    company_id: int
    trace_id: str
    authentication_method: AuthenticationMethod
    permissions: tuple[Permission, ...] = field(default_factory=tuple)
    user_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _required_text(self.user_id, "user_id is required."))
        _require_positive_int(self.company_id, "company_id must be positive.")
        object.__setattr__(self, "trace_id", _required_text(self.trace_id, "trace_id is required."))
        permissions = tuple(dict.fromkeys(self.permissions))
        for permission in permissions:
            if not isinstance(permission, Permission):
                raise InvalidAuthenticationContextError("permissions must contain canonical Permission values.")
        object.__setattr__(self, "permissions", permissions)
        if self.user_name is not None:
            normalized_user_name = self.user_name.strip()
            object.__setattr__(self, "user_name", normalized_user_name or None)


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    """Narrow request metadata passed from API dependencies to context resolvers."""

    headers: Mapping[str, str]


def _required_text(value: str | None, message: str) -> str:
    if value is None or not value.strip():
        raise InvalidAuthenticationContextError(message)
    return value.strip()


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise InvalidAuthenticationContextError(message)
