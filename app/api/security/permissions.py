from __future__ import annotations

from app.api.security.context import Permission, RequestContext
from app.api.security.exceptions import PermissionDeniedError


def require_permission(permission: Permission):
    """Return a guard that validates one permission on a resolved RequestContext."""

    def guard(context: RequestContext) -> RequestContext:
        if permission not in context.permissions:
            raise PermissionDeniedError("Permission is required.")
        return context

    return guard
