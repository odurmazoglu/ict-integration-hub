from __future__ import annotations

from app.api.security.context import AuthenticationMethod, Permission, RequestContext, RequestMetadata
from app.api.security.exceptions import (
    DevelopmentAuthenticationDisabledError,
    InvalidAuthenticationContextError,
)
from app.api.security.trace import HEADER_TRACE_ID, parse_trace_id
from app.core.config import Settings

HEADER_USER_ID = "x-ipp-user-id"
HEADER_USER_NAME = "x-ipp-user-name"
HEADER_COMPANY_ID = "x-ipp-company-id"
HEADER_PERMISSIONS = "x-ipp-permissions"


class DevelopmentHeaderRequestContextResolver:
    """Development-only request context resolver backed by explicit IPP headers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, metadata: RequestMetadata) -> RequestContext:
        if self._settings.ipp_auth_mode != "development_headers":
            raise DevelopmentAuthenticationDisabledError("Development header authentication is disabled.")
        if not self._settings.ipp_enable_development_header_auth:
            raise DevelopmentAuthenticationDisabledError("Development header authentication is disabled.")
        if self._settings.app_env == "production":
            raise DevelopmentAuthenticationDisabledError("Development header authentication is disabled in production.")

        headers = _normalized_headers(metadata)
        user_id = _required_header(headers, HEADER_USER_ID)
        company_id = _parse_company_id(_required_header(headers, HEADER_COMPANY_ID))
        permissions = _parse_permissions(headers.get(HEADER_PERMISSIONS, ""))
        trace_id = parse_trace_id(headers.get(HEADER_TRACE_ID))
        user_name = _optional_header(headers, HEADER_USER_NAME)
        return RequestContext(
            user_id=user_id,
            user_name=user_name,
            company_id=company_id,
            permissions=permissions,
            trace_id=trace_id,
            authentication_method=AuthenticationMethod.DEVELOPMENT_HEADERS,
        )


def _normalized_headers(metadata: RequestMetadata) -> dict[str, str]:
    return {str(key).lower(): str(value).strip() for key, value in metadata.headers.items()}


def _required_header(headers: dict[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None or not value.strip():
        raise InvalidAuthenticationContextError("Required authentication header is missing.")
    return value.strip()


def _optional_header(headers: dict[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _parse_company_id(value: str) -> int:
    try:
        company_id = int(value)
    except ValueError as exc:
        raise InvalidAuthenticationContextError("Company authentication context is invalid.") from exc
    if company_id <= 0:
        raise InvalidAuthenticationContextError("Company authentication context is invalid.")
    return company_id


def _parse_permissions(value: str) -> tuple[Permission, ...]:
    permissions: list[Permission] = []
    seen: set[Permission] = set()
    for raw_permission in value.split(","):
        normalized = raw_permission.strip()
        if not normalized:
            continue
        try:
            permission = Permission(normalized)
        except ValueError as exc:
            raise InvalidAuthenticationContextError("Permission authentication context is invalid.") from exc
        if permission not in seen:
            seen.add(permission)
            permissions.append(permission)
    return tuple(permissions)
