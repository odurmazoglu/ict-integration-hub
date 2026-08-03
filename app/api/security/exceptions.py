from __future__ import annotations


class SecurityContextError(RuntimeError):
    """Safe base error for API request-context authentication and authorization failures."""

    error_category = "security_context_error"

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class AuthenticationRequiredError(SecurityContextError):
    """Raised when no supported request authentication context is available."""

    error_category = "authentication_required"


class InvalidAuthenticationContextError(SecurityContextError):
    """Raised when request authentication metadata is present but invalid."""

    error_category = "invalid_authentication_context"


class PermissionDeniedError(SecurityContextError):
    """Raised when a request context does not grant the required permission."""

    error_category = "permission_denied"


class DevelopmentAuthenticationDisabledError(SecurityContextError):
    """Raised when development-header authentication is not explicitly enabled."""

    error_category = "development_authentication_disabled"


class InvalidTokenError(SecurityContextError):
    """Raised when a bearer JWT cannot be safely validated."""

    error_category = "invalid_token"


class TokenExpiredError(InvalidTokenError):
    """Raised when a bearer JWT is expired."""

    error_category = "token_expired"


class TokenIssuerError(InvalidTokenError):
    """Raised when a bearer JWT issuer is invalid."""

    error_category = "token_issuer_error"


class TokenAudienceError(InvalidTokenError):
    """Raised when a bearer JWT audience is invalid."""

    error_category = "token_audience_error"


class TokenSignatureError(InvalidTokenError):
    """Raised when a bearer JWT signature cannot be validated."""

    error_category = "token_signature_error"


class OidcConfigurationError(SecurityContextError):
    """Raised when OIDC discovery or JWKS configuration is invalid."""

    error_category = "oidc_configuration_error"


class OidcProviderUnavailableError(SecurityContextError):
    """Raised when the OIDC provider metadata or JWKS endpoint is unavailable."""

    error_category = "oidc_provider_unavailable"
