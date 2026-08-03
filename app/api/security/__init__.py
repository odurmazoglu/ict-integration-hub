"""API-layer security context foundation."""

from app.api.security.context import AuthenticationMethod, Permission, RequestContext, RequestMetadata
from app.api.security.development_headers import DevelopmentHeaderRequestContextResolver
from app.api.security.exceptions import (
    AuthenticationRequiredError,
    DevelopmentAuthenticationDisabledError,
    InvalidAuthenticationContextError,
    PermissionDeniedError,
    SecurityContextError,
)
from app.api.security.permissions import require_permission
from app.api.security.resolvers import DisabledRequestContextResolver, RequestContextResolver

__all__ = [
    "AuthenticationMethod",
    "AuthenticationRequiredError",
    "DevelopmentAuthenticationDisabledError",
    "DevelopmentHeaderRequestContextResolver",
    "DisabledRequestContextResolver",
    "InvalidAuthenticationContextError",
    "Permission",
    "PermissionDeniedError",
    "RequestContext",
    "RequestContextResolver",
    "RequestMetadata",
    "SecurityContextError",
    "require_permission",
]
