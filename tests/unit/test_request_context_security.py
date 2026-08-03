from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest
from starlette.datastructures import Headers

from app.api.dependencies import get_request_context, get_request_context_resolver
from app.api.security import (
    AuthenticationMethod,
    DevelopmentAuthenticationDisabledError,
    DevelopmentHeaderRequestContextResolver,
    DisabledRequestContextResolver,
    InvalidAuthenticationContextError,
    Permission,
    PermissionDeniedError,
    RequestContext,
    RequestMetadata,
    require_permission,
)
from app.core.config import Settings
from app.core.runtime_checks import runtime_configuration_errors


def test_request_context_is_immutable() -> None:
    context = _context()

    with pytest.raises(FrozenInstanceError):
        context.user_id = "changed"


def test_request_context_requires_positive_company_id() -> None:
    with pytest.raises(InvalidAuthenticationContextError):
        _context(company_id=0)


def test_request_context_requires_user_id() -> None:
    with pytest.raises(InvalidAuthenticationContextError):
        _context(user_id=" ")


def test_request_context_requires_trace_id() -> None:
    with pytest.raises(InvalidAuthenticationContextError):
        _context(trace_id="")


def test_request_context_permissions_are_immutable_and_deduplicated() -> None:
    context = _context(
        permissions=(Permission.WORKBENCH_REVIEW_READ, Permission.WORKBENCH_REVIEW_READ),
    )

    assert context.permissions == (Permission.WORKBENCH_REVIEW_READ,)
    with pytest.raises(AttributeError):
        context.permissions.append(Permission.WORKBENCH_REVIEW_DECIDE)  # type: ignore[attr-defined]


def test_authentication_method_vocabulary_is_canonical() -> None:
    assert {method.value for method in AuthenticationMethod} == {
        "development_headers",
        "jwt",
        "oauth2",
        "azure_ad",
        "odoo_session",
        "service_account",
    }


def test_permission_vocabulary_is_canonical() -> None:
    assert {permission.value for permission in Permission} == {
        "workbench_review_read",
        "workbench_review_decide",
    }


def test_valid_development_headers_resolve_context() -> None:
    context = _resolve(
        {
            "X-IPP-User-ID": " finance.user ",
            "X-IPP-User-Name": " Finance User ",
            "X-IPP-Company-ID": "7",
            "X-IPP-Permissions": "workbench_review_read, workbench_review_decide",
            "X-Trace-ID": "trace-123",
        }
    )

    assert context.user_id == "finance.user"
    assert context.user_name == "Finance User"
    assert context.company_id == 7
    assert context.permissions == (Permission.WORKBENCH_REVIEW_READ, Permission.WORKBENCH_REVIEW_DECIDE)
    assert context.trace_id == "trace-123"
    assert context.authentication_method is AuthenticationMethod.DEVELOPMENT_HEADERS


def test_missing_user_header_is_rejected() -> None:
    headers = _valid_headers()
    headers.pop("X-IPP-User-ID")

    with pytest.raises(InvalidAuthenticationContextError):
        _resolve(headers)


def test_missing_company_header_is_rejected() -> None:
    headers = _valid_headers()
    headers.pop("X-IPP-Company-ID")

    with pytest.raises(InvalidAuthenticationContextError):
        _resolve(headers)


def test_malformed_company_header_is_rejected_without_leaking_header_value() -> None:
    with pytest.raises(InvalidAuthenticationContextError) as error:
        _resolve({**_valid_headers(), "X-IPP-Company-ID": "company=secret"})

    assert str(error.value) == "Company authentication context is invalid."
    assert "secret" not in str(error.value)


def test_zero_or_negative_company_header_is_rejected() -> None:
    for value in ("0", "-1"):
        with pytest.raises(InvalidAuthenticationContextError):
            _resolve({**_valid_headers(), "X-IPP-Company-ID": value})


def test_unknown_permission_is_rejected_without_leaking_header_value() -> None:
    with pytest.raises(InvalidAuthenticationContextError) as error:
        _resolve({**_valid_headers(), "X-IPP-Permissions": "root,password=secret"})

    assert str(error.value) == "Permission authentication context is invalid."
    assert "secret" not in str(error.value)
    assert "root" not in str(error.value)


def test_duplicate_permissions_are_deduplicated() -> None:
    context = _resolve(
        {
            **_valid_headers(),
            "X-IPP-Permissions": "workbench_review_read,workbench_review_read,workbench_review_decide",
        }
    )

    assert context.permissions == (Permission.WORKBENCH_REVIEW_READ, Permission.WORKBENCH_REVIEW_DECIDE)


def test_user_name_header_is_optional() -> None:
    headers = _valid_headers()
    headers.pop("X-IPP-User-Name")

    context = _resolve(headers)

    assert context.user_name is None


def test_valid_inbound_trace_id_is_preserved() -> None:
    context = _resolve({**_valid_headers(), "X-Trace-ID": "trace.prod-123:abc"})

    assert context.trace_id == "trace.prod-123:abc"


def test_missing_trace_id_is_generated() -> None:
    headers = _valid_headers()
    headers.pop("X-Trace-ID")

    context = _resolve(headers)

    UUID(context.trace_id)


def test_unsafe_or_overlong_trace_id_is_rejected_without_leaking_value() -> None:
    for trace_id in ("trace with spaces token=secret", "a" * 129):
        with pytest.raises(InvalidAuthenticationContextError) as error:
            _resolve({**_valid_headers(), "X-Trace-ID": trace_id})

        assert str(error.value) == "Trace authentication context is invalid."
        assert "secret" not in str(error.value)
        assert "a" * 129 not in str(error.value)


def test_development_authentication_is_disabled_by_default() -> None:
    settings = Settings()
    resolver = get_request_context_resolver(settings)

    assert isinstance(resolver, DisabledRequestContextResolver)
    with pytest.raises(DevelopmentAuthenticationDisabledError):
        resolver.resolve(RequestMetadata(headers=Headers(_valid_headers())))


def test_development_authentication_is_rejected_in_production() -> None:
    settings = Settings(
        app_env="production",
        ipp_auth_mode="development_headers",
        ipp_enable_development_header_auth=True,
    )

    assert "IPP_ENABLE_DEVELOPMENT_HEADER_AUTH must be disabled in production." in runtime_configuration_errors(
        settings
    )
    with pytest.raises(DevelopmentAuthenticationDisabledError):
        DevelopmentHeaderRequestContextResolver(settings).resolve(RequestMetadata(headers=Headers(_valid_headers())))


def test_explicit_development_environment_allows_development_headers() -> None:
    settings = Settings(
        app_env="development",
        ipp_auth_mode="development_headers",
        ipp_enable_development_header_auth=True,
    )
    resolver = get_request_context_resolver(settings)

    assert isinstance(resolver, DevelopmentHeaderRequestContextResolver)
    assert resolver.resolve(RequestMetadata(headers=Headers(_valid_headers()))).company_id == 7


def test_no_anonymous_fallback_exists() -> None:
    with pytest.raises(DevelopmentAuthenticationDisabledError):
        DisabledRequestContextResolver().resolve(RequestMetadata(headers=Headers({})))


def test_permission_guard_allows_granted_permission() -> None:
    context = _context(permissions=(Permission.WORKBENCH_REVIEW_READ,))

    assert require_permission(Permission.WORKBENCH_REVIEW_READ)(context) is context


def test_permission_guard_rejects_missing_permission() -> None:
    with pytest.raises(PermissionDeniedError) as error:
        require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(
            _context(permissions=(Permission.WORKBENCH_REVIEW_READ,))
        )

    assert str(error.value) == "Permission is required."


def test_fastapi_dependency_uses_request_headers_only() -> None:
    request = FakeRequest(headers=Headers(_valid_headers()))
    resolver = DevelopmentHeaderRequestContextResolver(
        Settings(
            app_env="development",
            ipp_auth_mode="development_headers",
            ipp_enable_development_header_auth=True,
        )
    )

    context = get_request_context(request, resolver)

    assert context.company_id == 7
    assert "query_params" not in Path("app/api/dependencies.py").read_text(encoding="utf-8")
    assert "path_params" not in Path("app/api/dependencies.py").read_text(encoding="utf-8")


def test_security_context_contains_no_raw_tokens_or_headers() -> None:
    context = _resolve({**_valid_headers(), "Authorization": "Bearer secret-token"})

    values = str(context)
    assert "secret-token" not in values
    assert "authorization" not in values.lower()
    assert not hasattr(context, "headers")
    assert not hasattr(context, "token")
    assert not hasattr(context, "password")


def test_security_contracts_do_not_import_sqlalchemy_or_provider_boundaries() -> None:
    source = "\n".join(path.read_text(encoding="utf-8").lower() for path in Path("app/api/security").rglob("*.py"))
    forbidden = (
        "sqlalchemy",
        "app.models",
        "app.persistence",
        "app.connectors",
        "app.erp",
        "odoojson2client",
        "uyumsoftsoapclient",
        "account.move",
        "action_post",
        "workflowstrategy",
        "decisionengine",
        "authlib",
        "azure.identity",
        "msal",
    )

    for token in forbidden:
        assert token not in source


def test_request_context_contains_no_fastapi_request_object() -> None:
    assert "fastapi" not in Path("app/api/security/context.py").read_text(encoding="utf-8").lower()
    assert not hasattr(_context(), "request")


def test_application_layer_does_not_import_api_security_modules() -> None:
    source = "\n".join(path.read_text(encoding="utf-8").lower() for path in Path("app/application").rglob("*.py"))

    assert "app.api.security" not in source
    assert "fastapi" not in source


def test_permission_checks_do_not_import_repositories_or_workbench_persistence() -> None:
    source = Path("app/api/security/permissions.py").read_text(encoding="utf-8").lower()

    for token in ("app.persistence", "app.models", "sqlalchemy", "workbench_review_repository"):
        assert token not in source


def test_no_workbench_route_added() -> None:
    router_sources = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in Path("app/api/routers").glob("*.py")
    )

    assert "workbench" not in router_sources


def test_existing_health_endpoint_remains_unchanged() -> None:
    health_source = Path("app/api/routers/health.py").read_text(encoding="utf-8")

    assert 'return {"status": "ok"}' in health_source


class FakeRequest:
    def __init__(self, *, headers: Headers) -> None:
        self.headers = headers
        self.query_params = {"company_id": "999"}
        self.path_params = {"company_id": "999"}


def _resolve(headers: dict[str, str]) -> RequestContext:
    resolver = DevelopmentHeaderRequestContextResolver(
        Settings(
            app_env="development",
            ipp_auth_mode="development_headers",
            ipp_enable_development_header_auth=True,
        )
    )
    return resolver.resolve(RequestMetadata(headers=Headers(headers)))


def _valid_headers() -> dict[str, str]:
    return {
        "X-IPP-User-ID": "finance.user",
        "X-IPP-User-Name": "Finance User",
        "X-IPP-Company-ID": "7",
        "X-IPP-Permissions": "workbench_review_read",
        "X-Trace-ID": "trace-123",
    }


def _context(
    *,
    user_id: str = "finance.user",
    company_id: int = 7,
    trace_id: str = "trace-123",
    permissions: tuple[Permission, ...] = (Permission.WORKBENCH_REVIEW_READ,),
) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        user_name="Finance User",
        company_id=company_id,
        permissions=permissions,
        trace_id=trace_id,
        authentication_method=AuthenticationMethod.DEVELOPMENT_HEADERS,
    )
