from __future__ import annotations

from typing import Protocol

from app.api.security.context import RequestContext, RequestMetadata
from app.api.security.exceptions import DevelopmentAuthenticationDisabledError


class RequestContextResolver(Protocol):
    """API security boundary for resolving trusted request identity."""

    def resolve(self, metadata: RequestMetadata) -> RequestContext:
        pass


class DisabledRequestContextResolver:
    """Resolver used when no authentication adapter is explicitly enabled."""

    def resolve(self, metadata: RequestMetadata) -> RequestContext:
        raise DevelopmentAuthenticationDisabledError("Development header authentication is disabled.")
