# Keycloak OIDC Adapter

ICT IPP uses standard OIDC discovery and JWKS validation. The adapter is named `OidcJwtRequestContextResolver` because Keycloak is an identity-provider implementation detail, not an Application-layer concept.

## Required Settings

```text
IPP_AUTH_MODE=oidc_jwt
IPP_OIDC_ISSUER=https://<keycloak-host>/realms/<realm>
IPP_OIDC_AUDIENCE=<api-client-audience>
IPP_OIDC_DISCOVERY_URL=
IPP_OIDC_JWKS_URL=
IPP_OIDC_COMPANY_ID_CLAIM=ipp_company_id
IPP_OIDC_PERMISSIONS_CLAIM=ipp_permissions
IPP_OIDC_USERNAME_CLAIM=preferred_username
IPP_OIDC_ALLOWED_ALGORITHMS=["RS256"]
```

When `IPP_OIDC_DISCOVERY_URL` is empty, IPP derives it from the issuer as:

```text
<issuer>/.well-known/openid-configuration
```

When `IPP_OIDC_JWKS_URL` is empty, IPP uses the `jwks_uri` published by discovery.

## Token Contract

Access tokens presented to IPP must include:

- `iss`: equal to `IPP_OIDC_ISSUER`
- `aud`: contains or equals `IPP_OIDC_AUDIENCE`
- `sub`: stable user identifier
- `exp`: expiry timestamp
- `iat`: issued-at timestamp
- `ipp_company_id`: one positive integer company id
- `ipp_permissions`: JSON array of canonical permission strings

Optional:

- `preferred_username`: displayed user name, unless `IPP_OIDC_USERNAME_CLAIM` is changed

Current permission values:

- `workbench_review_read`
- `workbench_review_decide`

## Keycloak Mapper Guidance

Configure a client or client-scope mapper that emits `ipp_company_id` as a single integer-valued claim for the active IPP company. Configure another mapper that emits `ipp_permissions` as a JSON array of canonical permission strings.

Do not encode company identity in query strings, request bodies, path parameters, or untrusted headers for future authenticated routes. Route adapters must derive trusted identity from `RequestContext`.

## Security Behavior

IPP validates signatures through JWKS, rejects unsigned tokens, rejects unsupported algorithms, verifies issuer and audience, and fails closed when discovery or JWKS is unavailable. JWKS responses are cached for a bounded TTL and refreshed once when an otherwise-valid token references an unknown `kid`.

Errors returned by the resolver use safe text. Tokens, raw provider responses, credentials, URLs with secrets, and stack traces must not be exposed.

## Odoo Online Workbench Projection Boundary

Keycloak protects direct clients of the Hub REST API. The Odoo Online Workbench projection architecture does not install Odoo-side Python code that logs into Keycloak or calls the Hub with user bearer tokens.

Future projection synchronization is Hub-to-Odoo through Odoo JSON-2 using a restricted Odoo API key. Odoo Studio fields must not store Keycloak tokens, Keycloak client secrets, Hub bearer tokens, or Hub service credentials. A future Odoo SSO or login-federation decision is separate from Workbench projection synchronization.

## Out Of Scope

This adapter does not implement browser login, OAuth authorization-code flow, refresh tokens, user provisioning, role administration, Odoo SSO, Workbench projection synchronization, workflow execution, ERP writes, or provider-specific Keycloak APIs.
