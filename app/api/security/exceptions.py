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
