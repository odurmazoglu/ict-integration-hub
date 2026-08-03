from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.datastructures import Headers

from app.api.dependencies import get_request_context_resolver
from app.api.security import (
    AuthenticationMethod,
    AuthenticationRequiredError,
    InvalidAuthenticationContextError,
    InvalidTokenError,
    OidcConfigurationError,
    OidcJwtRequestContextResolver,
    OidcProviderUnavailableError,
    Permission,
    PermissionDeniedError,
    RequestMetadata,
    require_permission,
)
from app.api.security.exceptions import (
    TokenAudienceError,
    TokenExpiredError,
    TokenIssuerError,
    TokenSignatureError,
)
from app.core.config import Settings
from app.core.runtime_checks import (
    PRODUCTION_APPROVAL_ACK,
    runtime_configuration_errors,
    validate_runtime_configuration,
)

ISSUER = "https://idp.example.com/realms/ict"
AUDIENCE = "ict-integration-hub"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
SENSITIVE_TOKEN_TEXT = "token-secret-fragment"


def test_valid_signed_access_token_resolves_request_context() -> None:
    private_key, jwk = _rsa_key("key-1")
    resolver = _resolver(jwk)
    token = _token(private_key, kid="key-1")

    context = resolver.resolve(_metadata(token, trace_id="trace.prod-123"))

    assert context.user_id == "user-123"
    assert context.user_name == "Finance User"
    assert context.company_id == 7
    assert context.permissions == (Permission.WORKBENCH_REVIEW_READ, Permission.WORKBENCH_REVIEW_DECIDE)
    assert context.trace_id == "trace.prod-123"
    assert context.authentication_method is AuthenticationMethod.JWT
    assert not hasattr(context, "token")
    assert token not in str(context)


def test_permissions_claim_may_be_empty() -> None:
    private_key, jwk = _rsa_key("key-1")
    context = _resolver(jwk).resolve(_metadata(_token(private_key, kid="key-1", ipp_permissions=[])))

    assert context.permissions == ()


def test_duplicate_permissions_are_deduplicated() -> None:
    private_key, jwk = _rsa_key("key-1")
    token = _token(
        private_key,
        kid="key-1",
        ipp_permissions=["workbench_review_read", "workbench_review_read"],
    )

    context = _resolver(jwk).resolve(_metadata(token))

    assert context.permissions == (Permission.WORKBENCH_REVIEW_READ,)


def test_permission_guard_accepts_jwt_derived_permissions() -> None:
    private_key, jwk = _rsa_key("key-1")
    context = _resolver(jwk).resolve(_metadata(_token(private_key, kid="key-1")))

    assert require_permission(Permission.WORKBENCH_REVIEW_READ)(context) is context


def test_permission_guard_rejects_missing_jwt_permission() -> None:
    private_key, jwk = _rsa_key("key-1")
    context = _resolver(jwk).resolve(_metadata(_token(private_key, kid="key-1", ipp_permissions=[])))

    with pytest.raises(PermissionDeniedError):
        require_permission(Permission.WORKBENCH_REVIEW_READ)(context)


def test_missing_authorization_header_is_rejected() -> None:
    with pytest.raises(AuthenticationRequiredError):
        _resolver(_rsa_key("key-1")[1]).resolve(RequestMetadata(headers=Headers({})))


def test_wrong_authorization_scheme_is_rejected() -> None:
    with pytest.raises(AuthenticationRequiredError):
        _resolver(_rsa_key("key-1")[1]).resolve(RequestMetadata(headers=Headers({"Authorization": "Basic abc"})))


def test_empty_bearer_token_is_rejected() -> None:
    with pytest.raises(AuthenticationRequiredError):
        _resolver(_rsa_key("key-1")[1]).resolve(RequestMetadata(headers=Headers({"Authorization": "Bearer   "})))


def test_multiple_authorization_headers_are_rejected_where_detectable() -> None:
    headers = Headers(raw=[(b"authorization", b"Bearer one"), (b"authorization", b"Bearer two")])

    with pytest.raises(InvalidTokenError):
        _resolver(_rsa_key("key-1")[1]).resolve(RequestMetadata(headers=headers))


def test_malformed_jwt_is_rejected_without_leaking_token_text() -> None:
    with pytest.raises(InvalidTokenError) as error:
        _resolver(_rsa_key("key-1")[1]).resolve(_metadata(f"not-a-jwt.{SENSITIVE_TOKEN_TEXT}.value"))

    assert str(error.value) == "Bearer token is invalid."
    assert SENSITIVE_TOKEN_TEXT not in str(error.value)


def test_unsigned_token_is_rejected() -> None:
    token = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": "key-1"})

    with pytest.raises(InvalidTokenError):
        _resolver(_rsa_key("key-1")[1]).resolve(_metadata(token))


def test_disallowed_algorithm_is_rejected() -> None:
    token = jwt.encode(_claims(), "not-a-real-shared-secret-32-bytes", algorithm="HS256", headers={"kid": "key-1"})

    with pytest.raises(InvalidTokenError):
        _resolver(_rsa_key("key-1")[1]).resolve(_metadata(token))


def test_invalid_signature_is_rejected() -> None:
    signing_key, _ = _rsa_key("key-1")
    _, published_jwk = _rsa_key("key-1")
    token = _token(signing_key, kid="key-1")

    with pytest.raises(TokenSignatureError):
        _resolver(published_jwk).resolve(_metadata(token))


def test_expired_token_is_rejected() -> None:
    private_key, jwk = _rsa_key("key-1")
    token = _token(private_key, kid="key-1", exp=_timestamp(minutes=-5))

    with pytest.raises(TokenExpiredError):
        _resolver(jwk).resolve(_metadata(token))


def test_future_nbf_token_is_rejected() -> None:
    private_key, jwk = _rsa_key("key-1")
    token = _token(private_key, kid="key-1", nbf=_timestamp(minutes=10))

    with pytest.raises(InvalidTokenError):
        _resolver(jwk).resolve(_metadata(token))


def test_wrong_issuer_is_rejected() -> None:
    private_key, jwk = _rsa_key("key-1")
    token = _token(private_key, kid="key-1", iss="https://other.example.com/realms/ict")

    with pytest.raises(TokenIssuerError):
        _resolver(jwk).resolve(_metadata(token))


def test_wrong_audience_is_rejected() -> None:
    private_key, jwk = _rsa_key("key-1")
    token = _token(private_key, kid="key-1", aud="other-api")

    with pytest.raises(TokenAudienceError):
        _resolver(jwk).resolve(_metadata(token))


@pytest.mark.parametrize("claim", ["sub", "ipp_company_id"])
def test_required_claims_are_rejected(claim: str) -> None:
    private_key, jwk = _rsa_key("key-1")
    claims = _claims()
    del claims[claim]

    with pytest.raises((InvalidAuthenticationContextError, InvalidTokenError)):
        _resolver(jwk).resolve(_metadata(_token(private_key, kid="key-1", claims=claims)))


@pytest.mark.parametrize("company_claim", ["0", "-1", "company=secret", ["7"]])
def test_malformed_company_claim_is_rejected_without_leaking_value(company_claim: object) -> None:
    private_key, jwk = _rsa_key("key-1")

    with pytest.raises(InvalidAuthenticationContextError) as error:
        _resolver(jwk).resolve(_metadata(_token(private_key, kid="key-1", ipp_company_id=company_claim)))

    assert str(error.value) == "Token company claim is invalid."
    assert "secret" not in str(error.value)


def test_unknown_permission_is_rejected_without_leaking_value() -> None:
    private_key, jwk = _rsa_key("key-1")

    with pytest.raises(InvalidAuthenticationContextError) as error:
        _resolver(jwk).resolve(_metadata(_token(private_key, kid="key-1", ipp_permissions=["root", "password=secret"])))

    assert str(error.value) == "Token permissions claim is invalid."
    assert "root" not in str(error.value)
    assert "secret" not in str(error.value)


def test_discovery_issuer_mismatch_is_rejected() -> None:
    private_key, jwk = _rsa_key("key-1")
    resolver = _resolver(jwk, discovery_issuer="https://wrong.example.com/realms/ict")

    with pytest.raises(OidcConfigurationError):
        resolver.resolve(_metadata(_token(private_key, kid="key-1")))


def test_provider_unavailable_failure_is_safe_and_closed() -> None:
    private_key, _ = _rsa_key("key-1")
    resolver = OidcJwtRequestContextResolver(
        _settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(_raising_transport(httpx.ConnectError("url=secret")))),
    )

    with pytest.raises(OidcProviderUnavailableError) as error:
        resolver.resolve(_metadata(_token(private_key, kid="key-1")))

    assert str(error.value) == "OIDC discovery endpoint is unavailable."
    assert "secret" not in str(error.value)


def test_jwks_timeout_failure_is_safe_and_closed() -> None:
    private_key, _ = _rsa_key("key-1")
    resolver = OidcJwtRequestContextResolver(
        _settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(_timeout_jwks_transport())),
    )

    with pytest.raises(OidcProviderUnavailableError) as error:
        resolver.resolve(_metadata(_token(private_key, kid="key-1")))

    assert str(error.value) == "OIDC JWKS endpoint is unavailable."
    assert "secret" not in str(error.value)


def test_jwks_document_is_cached_for_bounded_ttl() -> None:
    private_key, jwk = _rsa_key("key-1")
    calls: list[str] = []
    resolver = _resolver(jwk, calls=calls)
    token = _token(private_key, kid="key-1")

    resolver.resolve(_metadata(token))
    resolver.resolve(_metadata(token))

    assert calls.count(DISCOVERY_URL) == 1
    assert calls.count(JWKS_URL) == 1


def test_unknown_kid_refreshes_jwks_once() -> None:
    private_key, jwk = _rsa_key("key-2")
    calls: list[str] = []
    jwks_payloads = [{"keys": []}, {"keys": [jwk]}]
    resolver = _resolver(jwk, calls=calls, jwks_payloads=jwks_payloads)

    context = resolver.resolve(_metadata(_token(private_key, kid="key-2")))

    assert context.user_id == "user-123"
    assert calls.count(JWKS_URL) == 2


def test_oidc_resolver_boundary_does_not_import_erp_or_workflow_layers() -> None:
    source = Path("app/api/security/oidc_jwt.py").read_text(encoding="utf-8").lower()

    for token in (
        "app.application",
        "app.connectors",
        "app.erp",
        "app.models",
        "app.persistence",
        "sqlalchemy",
        "odoo",
        "uyumsoft",
        "workflowstrategy",
        "decisionengine",
    ):
        assert token not in source


def test_get_request_context_resolver_selects_oidc_mode_without_fallback() -> None:
    resolver = get_request_context_resolver(_settings())

    assert isinstance(resolver, OidcJwtRequestContextResolver)
    with pytest.raises(AuthenticationRequiredError):
        resolver.resolve(RequestMetadata(headers=Headers({})))


def test_production_runtime_requires_oidc_jwt_auth_mode() -> None:
    settings = Settings(app_env="production")

    assert "IPP_AUTH_MODE must be oidc_jwt in production." in runtime_configuration_errors(settings)


def test_production_runtime_requires_https_oidc_urls() -> None:
    settings = Settings(
        app_env="production",
        ipp_auth_mode="oidc_jwt",
        ipp_oidc_issuer="http://idp.example.com/realms/ict",
        ipp_oidc_audience=AUDIENCE,
        ipp_oidc_discovery_url="http://idp.example.com/realms/ict/.well-known/openid-configuration",
        ipp_oidc_jwks_url="http://idp.example.com/realms/ict/protocol/openid-connect/certs",
    )

    errors = runtime_configuration_errors(settings)

    assert "IPP_OIDC_ISSUER must use HTTPS in production." in errors
    assert "IPP_OIDC_DISCOVERY_URL must use HTTPS in production." in errors
    assert "IPP_OIDC_JWKS_URL must use HTTPS in production." in errors


def test_production_runtime_accepts_complete_oidc_configuration(tmp_path: Path) -> None:
    validate_runtime_configuration(
        Settings(
            app_env="production",
            database_url="postgresql+psycopg://ict:<password>@db.internal:5432/ict",
            document_storage_root=tmp_path / "documents",
            production_operations_enabled=True,
            production_approval_ack=PRODUCTION_APPROVAL_ACK,
            ipp_auth_mode="oidc_jwt",
            ipp_oidc_issuer=ISSUER,
            ipp_oidc_audience=AUDIENCE,
            ipp_oidc_jwks_url=JWKS_URL,
            odoo_base_url="https://odoo.example-tenant.com",
            odoo_database="ict-prod",
            odoo_api_key="replace-with-real-secret",
            odoo_purchase_journal_id=10,
            uyumsoft_environment="production",
            uyumsoft_username="uyumsoft-prod-user",
            uyumsoft_password="replace-with-real-secret",
        )
    )


def _rsa_key(kid: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private_key, jwk


def _settings() -> Settings:
    return Settings(
        ipp_auth_mode="oidc_jwt",
        ipp_oidc_issuer=ISSUER,
        ipp_oidc_audience=AUDIENCE,
        ipp_oidc_jwks_cache_seconds=300,
    )


def _resolver(
    jwk: dict[str, Any],
    *,
    discovery_issuer: str = ISSUER,
    calls: list[str] | None = None,
    jwks_payloads: list[dict[str, Any]] | None = None,
) -> OidcJwtRequestContextResolver:
    return OidcJwtRequestContextResolver(
        _settings(),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                _oidc_transport(
                    jwk,
                    discovery_issuer=discovery_issuer,
                    calls=calls,
                    jwks_payloads=jwks_payloads,
                )
            )
        ),
    )


def _oidc_transport(
    jwk: dict[str, Any],
    *,
    discovery_issuer: str,
    calls: list[str] | None,
    jwks_payloads: list[dict[str, Any]] | None,
) -> Callable[[httpx.Request], httpx.Response]:
    payloads = list(jwks_payloads or [{"keys": [jwk]}])

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url == DISCOVERY_URL:
            return httpx.Response(200, json={"issuer": discovery_issuer, "jwks_uri": JWKS_URL})
        if url == JWKS_URL:
            payload = payloads.pop(0) if len(payloads) > 1 else payloads[0]
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return handler


def _raising_transport(exc: httpx.HTTPError) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _timeout_jwks_transport() -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DISCOVERY_URL:
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        raise httpx.TimeoutException("url=secret")

    return handler


def _metadata(token: str, *, trace_id: str = "trace-123") -> RequestMetadata:
    return RequestMetadata(headers=Headers({"Authorization": f"Bearer {token}", "X-Trace-ID": trace_id}))


def _token(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str,
    claims: dict[str, Any] | None = None,
    **overrides: object,
) -> str:
    payload = dict(claims or _claims())
    payload.update(overrides)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid, "typ": "Bearer"})


def _claims() -> dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "preferred_username": "Finance User",
        "ipp_company_id": 7,
        "ipp_permissions": ["workbench_review_read", "workbench_review_decide"],
        "iat": _timestamp(minutes=-1),
        "nbf": _timestamp(minutes=-1),
        "exp": _timestamp(minutes=10),
    }


def _timestamp(*, minutes: int) -> int:
    return int((datetime.now(UTC) + timedelta(minutes=minutes)).timestamp())
