# Security

ICT Integration Hub has an API-layer `RequestContext` foundation for future authenticated routes.

The current production adapter validates standard OIDC/JWT bearer tokens through discovery and JWKS. It is compatible with Keycloak when Keycloak emits the IPP claim contract documented below.

## RequestContext

`RequestContext` lives in the API security boundary, not the Application layer.

Fields:

- `user_id`
- `user_name`
- `company_id`
- `permissions`
- `trace_id`
- `authentication_method`

The context is immutable, contains no raw tokens, stores permissions as immutable canonical values, requires a positive `company_id`, requires non-empty `user_id` and `trace_id`, and does not carry FastAPI request objects, cookies, authorization headers, provider sessions, passwords, or credentials.

## Authentication Methods

Canonical vocabulary:

- `DEVELOPMENT_HEADERS`
- `JWT`
- `OAUTH2`
- `AZURE_AD`
- `ODOO_SESSION`
- `SERVICE_ACCOUNT`

Operational resolver modes:

- `disabled`: default for local development until an authentication mode is explicitly selected. Protected routes must fail closed.
- `development_headers`: temporary local/test authentication through explicit IPP headers.
- `oidc_jwt`: production authentication through standard OIDC discovery, JWKS, and signed bearer JWT validation.

`IPP_AUTH_MODE` selects exactly one resolver. Production requires `IPP_AUTH_MODE=oidc_jwt`.

## OIDC JWT

`OidcJwtRequestContextResolver` validates `Authorization: Bearer <token>` and never stores the raw token in `RequestContext`.

Validation includes:

- OIDC discovery issuer matching `IPP_OIDC_ISSUER`
- JWKS retrieval from discovery, or `IPP_OIDC_JWKS_URL` override
- bounded JWKS cache controlled by `IPP_OIDC_JWKS_CACHE_SECONDS`
- one JWKS refresh when a token references an unknown `kid`
- signature verification
- allowed algorithms from `IPP_OIDC_ALLOWED_ALGORITHMS`; `none` is rejected
- issuer and audience verification
- `exp`, `nbf`, `iat`, and `sub` validation with bounded clock skew
- safe exceptions for invalid token, expired token, issuer, audience, signature, provider unavailable, and OIDC configuration failures

Claim mapping:

- `sub` -> `RequestContext.user_id`
- `preferred_username` by default -> `RequestContext.user_name`
- `ipp_company_id` by default -> `RequestContext.company_id`
- `ipp_permissions` by default -> `RequestContext.permissions`
- `X-Trace-ID`, when safe, -> `RequestContext.trace_id`; otherwise a UUID is generated

The company claim must be exactly one positive integer. Permission claims must be a JSON array of canonical permission strings. Unknown permissions are rejected and duplicates are deduplicated.

Production runtime validation requires HTTPS OIDC issuer, discovery, and JWKS URLs. Provider errors and token validation failures are reported through sanitized exception text and preserve exception chaining internally.

## Development Headers

Development-header authentication is temporary and local/test-only.

Required headers when enabled:

- `X-IPP-User-ID`
- `X-IPP-Company-ID`

Optional headers:

- `X-IPP-User-Name`
- `X-IPP-Permissions`
- `X-Trace-ID`

`X-IPP-Permissions` is a comma-separated list of canonical permission values. Unknown values are rejected. Duplicate values are deduplicated.

`X-Trace-ID` is preserved only when it is short and uses the safe character set. If absent, a UUID is generated. Unsafe or overlong trace IDs are rejected with a safe error.

Development-header authentication is gated by:

```text
APP_ENV=development
IPP_AUTH_MODE=development_headers
IPP_ENABLE_DEVELOPMENT_HEADER_AUTH=true
```

The gate defaults to disabled. Production rejects development-header authentication during runtime validation and the resolver also refuses it.

## Permissions

Current permission vocabulary:

- `WORKBENCH_REVIEW_READ`
- `WORKBENCH_REVIEW_DECIDE`

Permissions are claims in `RequestContext`. This is not an RBAC system, role database, user database, or administration interface.

## Company Isolation

Future API adapters must derive company identity from `RequestContext.company_id`.

They must not accept trusted `company_id` from:

- request bodies
- query strings
- path parameters

Future Workbench decision routes must derive `decided_by` from `RequestContext.user_id`, not blindly trust client-provided user identity.

## Out Of Scope

Not implemented:

- login
- OAuth2
- Azure AD
- SSO
- Auth0
- Odoo session authentication
- API keys
- service account issuance
- user, role, or permission persistence
- Workbench API routes
- ERP writes
- workflow execution
- AI authentication or authorization behavior
