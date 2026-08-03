from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
import jwt
from jwt import PyJWK
from jwt.exceptions import (
    DecodeError,
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
)
from jwt.exceptions import (
    InvalidTokenError as PyJwtInvalidTokenError,
)

from app.api.security.context import AuthenticationMethod, Permission, RequestContext, RequestMetadata
from app.api.security.exceptions import (
    AuthenticationRequiredError,
    InvalidAuthenticationContextError,
    InvalidTokenError,
    OidcConfigurationError,
    OidcProviderUnavailableError,
    TokenAudienceError,
    TokenExpiredError,
    TokenIssuerError,
    TokenSignatureError,
)
from app.api.security.trace import HEADER_TRACE_ID, parse_trace_id
from app.core.config import Settings

HEADER_AUTHORIZATION = "authorization"
BEARER_PREFIX = "bearer"
DEFAULT_HTTP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class OidcProviderMetadata:
    issuer: str
    jwks_uri: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class JwksDocument:
    keys: tuple[dict[str, Any], ...]
    expires_at: float


class OidcJwtRequestContextResolver:
    """OIDC/JWKS-backed resolver for standard bearer access tokens."""

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http_client = http_client or httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
        self._provider_metadata: OidcProviderMetadata | None = None
        self._jwks: JwksDocument | None = None

    def resolve(self, metadata: RequestMetadata) -> RequestContext:
        token = _extract_bearer_token(metadata)
        claims = self._validate_token(token)
        return RequestContext(
            user_id=_required_text_claim(claims, "sub"),
            user_name=_optional_text_claim(claims, self._settings.ipp_oidc_username_claim),
            company_id=_company_id_from_claim(claims, self._settings.ipp_oidc_company_id_claim),
            permissions=_permissions_from_claim(claims, self._settings.ipp_oidc_permissions_claim),
            trace_id=parse_trace_id(_normalized_headers(metadata).get(HEADER_TRACE_ID)),
            authentication_method=AuthenticationMethod.JWT,
        )

    def _validate_token(self, token: str) -> dict[str, Any]:
        try:
            unverified_header = jwt.get_unverified_header(token)
        except DecodeError as exc:
            raise InvalidTokenError("Bearer token is invalid.") from exc
        algorithm = str(unverified_header.get("alg") or "")
        kid = str(unverified_header.get("kid") or "")
        if algorithm not in self._settings.ipp_oidc_allowed_algorithms:
            raise InvalidTokenError("Bearer token algorithm is not allowed.")
        if not kid:
            raise InvalidTokenError("Bearer token key id is missing.")

        signing_key = self._signing_key(kid)
        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=list(self._settings.ipp_oidc_allowed_algorithms),
                audience=self._settings.ipp_oidc_audience,
                issuer=self._settings.ipp_oidc_issuer,
                leeway=self._settings.ipp_oidc_clock_skew_seconds,
                options={
                    "require": ["exp", "iat", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except ExpiredSignatureError as exc:
            raise TokenExpiredError("Bearer token is expired.") from exc
        except ImmatureSignatureError as exc:
            raise InvalidTokenError("Bearer token is not yet valid.") from exc
        except InvalidIssuerError as exc:
            raise TokenIssuerError("Bearer token issuer is invalid.") from exc
        except InvalidAudienceError as exc:
            raise TokenAudienceError("Bearer token audience is invalid.") from exc
        except InvalidSignatureError as exc:
            raise TokenSignatureError("Bearer token signature is invalid.") from exc
        except PyJwtInvalidTokenError as exc:
            raise InvalidTokenError("Bearer token is invalid.") from exc
        if not isinstance(claims, dict):
            raise InvalidTokenError("Bearer token is invalid.")
        return claims

    def _signing_key(self, kid: str) -> Any:
        key = self._find_jwk(kid, refresh_on_miss=True)
        try:
            return PyJWK.from_dict(key).key
        except (PyJwtInvalidTokenError, ValueError, TypeError) as exc:
            raise OidcConfigurationError("OIDC signing key is invalid.") from exc

    def _find_jwk(self, kid: str, *, refresh_on_miss: bool) -> dict[str, Any]:
        jwks = self._get_jwks(force_refresh=False)
        key = _jwk_by_kid(jwks, kid)
        if key is not None:
            return key
        if refresh_on_miss:
            refreshed_jwks = self._get_jwks(force_refresh=True)
            key = _jwk_by_kid(refreshed_jwks, kid)
            if key is not None:
                return key
        raise TokenSignatureError("Bearer token signing key is unavailable.")

    def _get_jwks(self, *, force_refresh: bool) -> JwksDocument:
        now = monotonic()
        if not force_refresh and self._jwks is not None and self._jwks.expires_at > now:
            return self._jwks
        metadata = self._get_provider_metadata()
        try:
            response = self._http_client.get(metadata.jwks_uri)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcProviderUnavailableError("OIDC JWKS endpoint is unavailable.") from exc
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            raise OidcConfigurationError("OIDC JWKS document is invalid.")
        self._jwks = JwksDocument(
            keys=tuple(key for key in keys if isinstance(key, dict)),
            expires_at=now + self._settings.ipp_oidc_jwks_cache_seconds,
        )
        return self._jwks

    def _get_provider_metadata(self) -> OidcProviderMetadata:
        now = monotonic()
        if self._provider_metadata is not None and self._provider_metadata.expires_at > now:
            return self._provider_metadata
        discovery_url = _discovery_url(self._settings)
        try:
            response = self._http_client.get(discovery_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcProviderUnavailableError("OIDC discovery endpoint is unavailable.") from exc
        if not isinstance(payload, dict):
            raise OidcConfigurationError("OIDC discovery document is invalid.")
        issuer = str(payload.get("issuer") or "")
        jwks_uri = str(self._settings.ipp_oidc_jwks_url or payload.get("jwks_uri") or "")
        if issuer != self._settings.ipp_oidc_issuer:
            raise OidcConfigurationError("OIDC discovery issuer does not match configuration.")
        if not jwks_uri:
            raise OidcConfigurationError("OIDC JWKS URL is required.")
        self._provider_metadata = OidcProviderMetadata(
            issuer=issuer,
            jwks_uri=jwks_uri,
            expires_at=now + self._settings.ipp_oidc_jwks_cache_seconds,
        )
        return self._provider_metadata


def _extract_bearer_token(metadata: RequestMetadata) -> str:
    values = _authorization_values(metadata)
    if not values:
        raise AuthenticationRequiredError("Bearer authentication is required.")
    if len(values) > 1:
        raise InvalidTokenError("Authorization header is invalid.")
    authorization = values[0].strip()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != BEARER_PREFIX:
        raise AuthenticationRequiredError("Bearer authentication is required.")
    if not token.strip():
        raise AuthenticationRequiredError("Bearer authentication is required.")
    return token.strip()


def _authorization_values(metadata: RequestMetadata) -> list[str]:
    headers = metadata.headers
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        return [str(value) for value in getlist(HEADER_AUTHORIZATION)]
    normalized = _normalized_headers(metadata)
    value = normalized.get(HEADER_AUTHORIZATION)
    return [] if value is None else [value]


def _normalized_headers(metadata: RequestMetadata) -> dict[str, str]:
    return {str(key).lower(): str(value).strip() for key, value in metadata.headers.items()}


def _discovery_url(settings: Settings) -> str:
    if settings.ipp_oidc_discovery_url:
        return settings.ipp_oidc_discovery_url
    return f"{settings.ipp_oidc_issuer.rstrip('/')}/.well-known/openid-configuration"


def _jwk_by_kid(jwks: JwksDocument, kid: str) -> dict[str, Any] | None:
    for key in jwks.keys:
        if str(key.get("kid") or "") == kid:
            return key
    return None


def _required_text_claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidAuthenticationContextError("Required token claim is missing.")
    return value.strip()


def _optional_text_claim(claims: dict[str, Any], name: str) -> str | None:
    value = claims.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidAuthenticationContextError("Token username claim is invalid.")
    return value.strip() or None


def _company_id_from_claim(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if type(value) is not int:
        raise InvalidAuthenticationContextError("Token company claim is invalid.")
    if value <= 0:
        raise InvalidAuthenticationContextError("Token company claim is invalid.")
    return value


def _permissions_from_claim(claims: dict[str, Any], name: str) -> tuple[Permission, ...]:
    value = claims.get(name, [])
    if value is None:
        value = []
    if not isinstance(value, list):
        raise InvalidAuthenticationContextError("Token permissions claim is invalid.")
    permissions: list[Permission] = []
    seen: set[Permission] = set()
    for raw_permission in value:
        if not isinstance(raw_permission, str):
            raise InvalidAuthenticationContextError("Token permissions claim is invalid.")
        try:
            permission = Permission(raw_permission.strip())
        except ValueError as exc:
            raise InvalidAuthenticationContextError("Token permissions claim is invalid.") from exc
        if permission not in seen:
            seen.add(permission)
            permissions.append(permission)
    return tuple(permissions)
