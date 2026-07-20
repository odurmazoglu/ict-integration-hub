# Failure Injection

Failure injection validates that the system fails safely, logs safely, and recovers predictably. Run these tests only in local or approved non-production environments.

Do not use failure injection to call Uyumsoft or Odoo state-changing operations.

## Database Offline

- Setup: stop PostgreSQL or point the app to an unavailable test database.
- Exercise: start the app and call readiness; run a persistence workflow if appropriate.
- Expected failure: readiness fails or workflow returns a safe persistence error.
- Expected recovery: restore database connectivity, verify migrations/current revision, rerun the same bounded operation, and confirm idempotency prevents duplicates.

## Odoo Offline

- Setup: mock Odoo client timeout or point non-production config to an unavailable host.
- Exercise: run Odoo resolution or draft creation validation.
- Expected failure: safe connector error; no full payload or credentials in logs.
- Expected recovery: restore Odoo access, rerun read-only resolution, then draft creation only with explicit confirmation and reviewed ids.

## Uyumsoft Offline

- Setup: mock Uyumsoft connector transport failure or use an unavailable test endpoint in a controlled environment.
- Exercise: run bounded invoice listing or document download.
- Expected failure: safe connector timeout/transport error; failed sync run records progress where applicable.
- Expected recovery: restore provider access and rerun the same bounded window; ETTN/fallback identity prevents duplicates.

## Timeout

- Setup: configure mocks to exceed client timeout.
- Exercise: call sync, document download, Odoo resolution, or draft creation.
- Expected failure: timeout classification; bounded retry behavior for transient Uyumsoft transport failures only.
- Expected recovery: fix network/client timeout condition; rerun bounded operation.

## Invalid Credentials

- Setup: use placeholder or intentionally invalid non-production credentials.
- Exercise: run explicit provider smoke or connector call in a controlled environment.
- Expected failure: authentication/authorization failure without printing credentials.
- Expected recovery: restore valid credentials from secret storage; rerun smoke checks.

## Malformed XML

- Setup: store a synthetic malformed XML document through test fixtures or storage doubles.
- Exercise: parse the document.
- Expected failure: parser returns malformed XML error with document id, storage key, safe category, and field/path where available.
- Expected recovery: replace the fixture/document with valid XML; parse again.

## Invalid Configuration

- Setup: set unsafe production flags, placeholder credentials, localhost production database URL, or mismatched Uyumsoft production/test hosts.
- Exercise: start the app or call readiness.
- Expected failure: fail-fast startup/readiness error with setting names and safe policy messages only.
- Expected recovery: correct configuration in the environment or secret store; restart and verify readiness.

## Missing Environment Variables

- Setup: omit required database, provider, Odoo, or storage settings in local/non-production config.
- Exercise: start the app or call readiness.
- Expected failure: safe configuration validation error.
- Expected recovery: define missing values without committing secrets; restart and rerun health/readiness.

## Retry Validation

- Setup: mock first Uyumsoft transport call as transient failure and the next call as success.
- Exercise: run a bounded listing or download workflow.
- Expected behavior: retry succeeds within configured bounds; no duplicate persistence occurs.
- Negative check: SOAP faults, invalid credentials, parser errors, and validation errors are not retried as transient transport failures.

## Startup Validation

- Setup: run startup with safe local config, unsafe production config, and corrected production-like config.
- Exercise: start application and call `/health/live` and `/health/ready`.
- Expected behavior: live endpoint reflects process liveness; ready endpoint validates config, database, and storage without calling Uyumsoft or Odoo.

## Expected Recovery Evidence

Record for each failure injection:

- test date and environment
- injected failure
- command or request used
- observed safe error category
- retry behavior
- recovery action
- final result
- confirmation that logs did not contain secrets, full XML, SOAP payloads, or full Odoo payloads
