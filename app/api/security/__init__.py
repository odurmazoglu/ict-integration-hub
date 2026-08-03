"""API-layer security context foundation."""

from app.api.security.context import AuthenticationMethod, Permission, RequestContext, RequestMetadata
from app.api.security.development_headers import DevelopmentHeaderRequestContextResolver
from app.api.security.exceptions import (
    AuthenticationRequiredError,
    DevelopmentAuthenticationDisabledError,
    InvalidAuthenticationContextError,
    InvalidTokenError,
    OidcConfigurationError,
    OidcProviderUnavailableError,
    PermissionDeniedError,
    SecurityContextError,
    TokenAudienceError,
    TokenExpiredError,
    TokenIssuerError,
    TokenSignatureError,
)
from app.api.security.oidc_jwt import OidcJwtRequestContextResolver
from app.api.security.permissions import require_permission
from app.api.security.resolvers import DisabledRequestContextResolver, RequestContextResolver

__all__ = [
    "AuthenticationMethod",
    "AuthenticationRequiredError",
    "DevelopmentAuthenticationDisabledError",
    "DevelopmentHeaderRequestContextResolver",
    "DisabledRequestContextResolver",
    "InvalidAuthenticationContextError",
    "InvalidTokenError",
    "OidcConfigurationError",
    "OidcJwtRequestContextResolver",
    "OidcProviderUnavailableError",
    "Permission",
    "PermissionDeniedError",
    "RequestContext",
    "RequestContextResolver",
    "RequestMetadata",
    "SecurityContextError",
    "TokenAudienceError",
    "TokenExpiredError",
    "TokenIssuerError",
    "TokenSignatureError",
    "require_permission",
]
