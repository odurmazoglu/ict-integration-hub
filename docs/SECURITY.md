# Security

ICT Integration Hub now has an API-layer RequestContext foundation for future authenticated routes.

This foundation does not implement real authentication. It defines the stable boundary future authentication adapters will use to provide trusted user, company, permission, and trace identity to FastAPI route adapters.

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

Only `DEVELOPMENT_HEADERS` is operational in this foundation PR. The other values are vocabulary for future focused authentication adapters and do not decode tokens, call identity providers, or establish sessions.

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
IPP_ENABLE_DEVELOPMENT_HEADER_AUTH=1
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
- JWT verification
- OAuth2
- Azure AD
- SSO
- Keycloak
- Auth0
- Odoo session authentication
- API keys
- service account issuance
- user, role, or permission persistence
- Workbench API routes
- ERP writes
- workflow execution
- AI authentication or authorization behavior
