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

API adapters must derive company identity from `RequestContext.company_id`.

They must not accept trusted `company_id` from:

- request bodies
- query strings
- path parameters

Workbench decision routes must derive `decided_by` from `RequestContext.user_id`, not blindly trust client-provided user identity.

## Authenticated Workbench API

Implemented Workbench endpoints:

- `GET /api/workbench/reviews`
- `GET /api/workbench/reviews/{review_id}`
- `POST /api/workbench/reviews/{review_id}/decision`

Required permissions:

- queue and detail reads: `workbench_review_read`
- decision submission: `workbench_review_decide`

The route adapters construct `ReviewQueueQuery`, `ReviewDetailQuery`, and `ReviewDecisionCommand` with trusted identity from `RequestContext`. They do not accept `company_id`, `decided_by`, or body-level `review_id` from the client.

Workbench business context allocations are user-submitted evidence, not authorization. Allocation identifiers received from API clients or future Odoo projection child rows are untrusted until the Hub validates them. `target_company_id`, `customer_id`, `recharge_partner_id`, and `customer_invoice_id` do not grant cross-company access, do not prove ownership, and do not authorize customer invoice creation or recharge execution. The current contract performs structural positive-integer validation only; future repository validators must verify record existence, company access, outgoing customer-invoice/refund semantics, and partner/company relationships before execution.

Successful and failed Workbench responses include the same `trace_id` in the JSON body and `X-Trace-ID` response header. Authentication failures before `RequestContext` resolution use the validated inbound trace id when available, otherwise a safe generated id.

## Odoo Online Workbench Projection Security

Odoo 19 Online Workbench synchronization uses Hub-to-Odoo service authentication through Odoo JSON-2 and a restricted Odoo API key. It does not use an installed Odoo Python addon and does not store Hub bearer tokens, Keycloak tokens, Keycloak client secrets, or Odoo API keys in Odoo Studio fields.

The Odoo integration user must receive access only to the dedicated Workbench projection models and explicitly required future ERP models. This projection scope does not authorize accounting posting, payment creation, reconciliation, master-data mutation, deletion, procurement execution, or workflow execution.

The Odoo Workbench candidate reader is read-only. It uses JSON-2 `search_read` against configured Studio projection models, detects duplicate parent candidates, and translates provider failures into safe Workbench exceptions. It must not expose raw Odoo responses, URLs with secrets, credentials, bearer tokens, API keys, or stack traces.

Odoo user identity captured in the projection is audit evidence only. The Hub must validate `review_id`, `company_id`, expected version, idempotency key, selected workflow, and resolution details against Hub persistence before accepting a decision.

The Odoo Workbench decision submission orchestrator enforces requested company scope before submitting to Hub decision persistence. It preserves the Odoo candidate's expected version and idempotency key, does not accept client-supplied `decided_by`, and does not use Odoo user identity for authorization. It validates supported ERP references through read-only exact-ID repositories before decision persistence. It does not write back to Odoo, acknowledge projection fields, execute workflows, create ERP documents, or retry stale submissions.

ERP reference validation must not leak raw Odoo payloads, display names, URLs, credentials, tokens, SQL, provider exception text, or Studio field names. Failed validations use safe canonical messages. `target_company_id` is traceability context only; it does not authorize cross-company execution or override the requested company scope.

Workflow execution runtime is dry-run-first. The accepted-decision execution use case reads only canonical Hub decision evidence by exact review, company, and decision version, then derives the execution request internally. It persists execution snapshots, step state, checkpoints, retry policy, and append-only safe events in Hub SQL tables. Runtime transitions persist snapshot state and corresponding events atomically, allocate event sequence numbers inside the repository transaction, and reject stale snapshots through optimistic `runtime_version` checks. The application layer has no independent snapshot, checkpoint, or event mutation API. Runtime mutations are only legal through atomic execution creation or `persist_transition`. Execution idempotency is deterministic and separate from Workbench decision idempotency.

`EXECUTE` requires `EXECUTION_EXECUTE_ENABLED=true`, explicit `ExecutionApproval.approved_by`, `PRODUCTION_OPERATIONS_ENABLED=true` when a writer-backed strategy is present, and the configured production approval acknowledgement. Workbench `decided_by`, Odoo user identity, and projection audit fields are not execution approval. Approval acknowledgements for production writer gates are enforced by the writer policy and must not be persisted in runtime events. `VENDOR_BILL` execution may call `VendorBillWriter` to create or recover an Odoo Draft Vendor Bill. Existing-invoice `CUSTOMER_RECHARGE` execution is no-write and may only record `CUSTOMER_INVOICE` artifacts from accepted allocation evidence. Missing execution opt-in, missing approval, disabled writer gates for writer-backed plans, Customer Recharge allocations without `customer_invoice_id`, and unsupported steps are rejected before runtime creation and before any ERP writer call.

`customer_invoice_id` is non-unique evidence, not authorization and not a one-to-one relationship. It does not authorize customer invoice creation, posting, payment, reconciliation, settlement, collection, Sales Order mutation, analytic writes, or profitability posting. The existing-invoice Customer Recharge strategy does not call Odoo or reconstruct references from display names; it relies on already accepted and previously validated allocation evidence.

Execution source invoice reconstruction uses persisted Hub evidence only. The SQLAlchemy reader is pinned to accepted `review_id`, `company_id`, `decision_version`, and decision id, and it hydrates immutable invoice and matching DTOs without live Odoo reads, live Uyumsoft reads, rematching, fuzzy matching, AI, Workbench display text, raw provider payloads, or current ERP master-data refresh. Missing, malformed, cross-company, or ambiguous evidence fails closed with safe messages that do not expose raw XML, raw JSON, SQL, provider payloads, credentials, tokens, or URLs.

Pre-decision execution evidence is also Hub-owned persistence. `workbench_review_execution_evidence` stores immutable review-version snapshots before human decision submission, keyed by exact `company_id`, `review_id`, and `review_version`. Vendor Bill-capable review creation and pre-decision evidence insertion must be atomic so a decision-ready review is not visible without its authoritative evidence. Replays may reuse identical evidence but must not update historical evidence or accept changed evidence for the same review version.

Executable Vendor Bill decision acceptance and execution source evidence capture are atomic inside the Hub database. An accepted `SELECT_WORKFLOW + VENDOR_BILL` decision must not commit unless the matching schema-versioned evidence snapshot is inserted in the same transaction. Evidence is immutable and unique per accepted decision id; idempotent replay may read and verify identical evidence, but must not update, replace, or duplicate historical snapshots.

Hub runtime persistence and Odoo draft creation are a distributed write boundary and cannot be committed atomically together. Recovery depends on persisted runtime state, deterministic writer idempotency, and Odoo-side duplicate lookup before create. A lost Odoo response is recovered by rerunning the same incomplete runtime step, returning the existing Draft Vendor Bill, and completing the runtime without a duplicate draft. The execution runtime must not acknowledge Odoo projections, run in parallel, schedule background jobs, post accounting moves, register payments, reconcile, unlink, create customer invoices, create RFQs or Purchase Orders, or expose raw provider details.

Future decision ingestion must run as a Hub-controlled scheduler or service. Browser-side JavaScript must never receive Odoo API keys, Hub service tokens, or Keycloak client secrets.

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
- Odoo Studio projection synchronization
- Odoo decision ingestion
- Odoo SSO
- ERP writes
- workflow execution
- AI authentication or authorization behavior
