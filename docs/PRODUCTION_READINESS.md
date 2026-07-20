# Production Readiness

Production is disabled by default. Enabling production requires all runtime gates, environment separation, approvals, backups, and smoke validation below.

## Production Enablement Gates

The application fails fast when production configuration is unsafe. Production access requires all of the following:

- `APP_ENV=production`
- `PRODUCTION_OPERATIONS_ENABLED=true`
- `PRODUCTION_APPROVAL_ACK=APPROVED_FOR_PRODUCTION`
- `UYUMSOFT_ENVIRONMENT=production`
- `UYUMSOFT_PROD_WSDL_URL` host is `efatura.uyumsoft.com.tr`
- `UYUMSOFT_TEST_WSDL_URL` does not point to the production host
- `ODOO_BASE_URL` is not an example host
- `DATABASE_URL` does not point to localhost
- Odoo and Uyumsoft credentials are not placeholders

Outside production, `PRODUCTION_OPERATIONS_ENABLED` must be false and `PRODUCTION_APPROVAL_ACK` must be empty. `UYUMSOFT_ENVIRONMENT=production` is allowed only for explicit live-readonly validation when `LIVE_CONNECTOR_READONLY=true`, the production WSDL host is approved, the test WSDL is not pointed at production, and Uyumsoft credentials are not placeholders.

Odoo draft invoice creation remains draft-only. Production `action_post`, unlink, master-data mutation, and Uyumsoft state-changing operations remain forbidden.

## Configuration Validation

Startup and readiness validate safe configuration characteristics without printing secrets:

- application environment
- database URL presence and production localhost rejection
- Uyumsoft environment and WSDL host policy
- Uyumsoft credential presence
- Odoo base URL, database, API credential presence
- document storage path
- optional configured purchase journal id/code
- timeout and retry bounds
- production enablement and approval gates

Errors mention setting names and safe policy failures only. They must not include passwords, API keys, full database URLs, SOAP payloads, XML, or full invoice payloads.

## Environment Separation

Use separate environment profile files and secret stores per environment. The selected profile is explicit:

```bash
APP_ENV_FILE=.env.local docker compose up -d
APP_ENV_FILE=.env.test docker compose up -d
APP_ENV_FILE=.env.production docker compose up -d
APP_ENV_FILE=.env.live-readonly docker compose up -d
```

If `APP_ENV_FILE` is not set, `.env.local` is used. The application loads only one selected profile. It does not merge `.env`, `.env.local`, and `.env.production`, and `.env` is not implicitly preferred.

| Environment | Purpose | Provider access | Production gates |
| --- | --- | --- | --- |
| local development | local coding and mock tests | test or placeholders only | disabled |
| CI/test | automated unit tests | mocks only | disabled |
| staging/test provider | Uyumsoft test and non-production Odoo validation | test endpoints only | disabled |
| live-readonly validation | production Uyumsoft read-only validation with Odoo staging | Uyumsoft production read-only and Odoo staging | disabled |
| production | approved production operation | approved production endpoints only | explicitly enabled |

`APP_ENV` controls application runtime mode. Connector targets are separate: `UYUMSOFT_ENVIRONMENT` selects Uyumsoft `test` or `production`; Odoo staging/production is selected by `ODOO_BASE_URL`, `ODOO_DATABASE`, and the Odoo API key. Do not set `APP_ENV=production` merely because a connector targets a production host.

The supported live-readonly validation profile is:

```text
APP_ENV=development
LIVE_CONNECTOR_READONLY=true
ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1
PRODUCTION_OPERATIONS_ENABLED=false
PRODUCTION_APPROVAL_ACK=
UYUMSOFT_ENVIRONMENT=production
UYUMSOFT_TEST_WSDL_URL=https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl
UYUMSOFT_PROD_WSDL_URL=https://efatura.uyumsoft.com.tr/Services/Integration?wsdl
ODOO_BASE_URL=https://test-ictteknoloji.odoo.com
```

`ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1` only permits running the explicit read-only Uyumsoft smoke script. It never enables provider mutation, Odoo mutation, production operations, acknowledgement, accept/reject, status update, mark-as-read, send, retry, cancel, move-to-draft, or draft bill creation. `LIVE_CONNECTOR_READONLY=true` must still be set, `PRODUCTION_OPERATIONS_ENABLED` must remain false, and strict `APP_ENV=production` safety gates are unchanged.

`.env.local.example`, `.env.test.example`, `.env.production.example`, and `.env.live-readonly.example` are placeholder-only templates. Do not commit real profile files such as `.env.local`, `.env.test`, `.env.production`, or `.env.live-readonly`.

Do not source dotenv files directly in a shell. Prefer `APP_ENV_FILE=<profile> docker compose ...` or let `Settings` load the profile. If a dotenv file is ever sourced manually, quote values containing spaces.

## Health, Readiness, And Liveness

- `GET /health`: backward-compatible liveness response, returns `{"status":"ok"}`.
- `GET /health/live`: process liveness, no dependencies.
- `GET /health/ready`: readiness check for runtime configuration, database connectivity, and document storage writability.

Readiness does not call Uyumsoft or Odoo. Provider smoke checks remain explicit operational actions and must be read-only.

## Live-Readonly Uyumsoft Smoke Validation

The `.env.live-readonly` profile is the only example profile that sets `ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1`. All local, test, and production templates set it to `0`.

Run the smoke script with a narrow date range and `--page-size 1`:

```bash
APP_ENV_FILE=.env.live-readonly python3 scripts/uyumsoft_readonly_smoke.py \
  --from <iso-datetime> \
  --to <iso-datetime> \
  --page-size 1
```

Safety boundaries:

- `APP_ENV=development`
- `LIVE_CONNECTOR_READONLY=true`
- `ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1`
- `PRODUCTION_OPERATIONS_ENABLED=false`
- `PRODUCTION_APPROVAL_ACK=` empty
- `UYUMSOFT_ENVIRONMENT=production`
- only `GetInboxInvoiceList` and `GetOutboxInvoiceList` are used by the smoke script
- no provider write operation or Odoo write operation is enabled by the smoke flag

## Odoo Staging Connectivity Validation

Use the dedicated staging validation script before Resolution Validation or draft creation tests:

```bash
APP_ENV=development \
ODOO_BASE_URL=https://test-ictteknoloji.odoo.com \
ODOO_DATABASE=<staging-database> \
ODOO_API_KEY=<staging-api-key> \
ODOO_PURCHASE_JOURNAL_ID=<purchase-journal-id> \
python3 scripts/validate_odoo_staging.py --pretty
```

`ODOO_PURCHASE_JOURNAL_CODE=<purchase-journal-code>` can be used instead of `ODOO_PURCHASE_JOURNAL_ID`.

Expected output is a sanitized JSON report with the environment, target host, authentication status, database access status, company read status, per-model read status for `res.company`, `res.partner`, `product.product`, `account.tax`, `res.currency`, and `account.journal`, configured purchase journal status, permission/configuration failures, Resolution Validation blockers, and `no_write_operation_attempted=true`.

Safety guarantees:

- the script fails before any Odoo call unless `ODOO_BASE_URL` resolves to `test-ictteknoloji.odoo.com`
- `APP_ENV=production` is rejected
- only JSON-2 `search_read` probes and the existing read-only company probe are used
- `create`, `write`, `unlink`, and `action_post` are not attempted
- API keys, passwords, database URLs, full database names, raw JSON-2 payloads, and full master-data datasets are not included in the report

Troubleshooting:

- `configuration_failures`: fix the named environment variable; do not substitute production Odoo values
- `permission_failures`: grant least-privilege read access for the listed model in staging
- purchase journal `missing` or `ambiguous`: set exactly one of `ODOO_PURCHASE_JOURNAL_ID` or a unique purchase `ODOO_PURCHASE_JOURNAL_CODE`
- authentication failure: rotate or replace the staging API key without printing it in logs

## Uyumsoft Test Connectivity And UBL Acquisition Validation

Run the Uyumsoft acquisition validation only against the approved test WSDL host:

```bash
docker compose exec api python3 scripts/validate_uyumsoft_test.py --pretty --limit 5
```

Required environment variables:

- `UYUMSOFT_ENVIRONMENT=test`
- `UYUMSOFT_TEST_WSDL_URL=https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl`
- `UYUMSOFT_USERNAME=<test-username>`
- `UYUMSOFT_PASSWORD=<test-password>`
- `DATABASE_URL=<integration-hub-database-url>`
- `DOCUMENT_STORAGE_ROOT=<local-or-mounted-document-storage-path>`

Optional controls:

- `--from-date <iso-datetime>`
- `--to-date <iso-datetime>`
- `--limit <1-100>`
- `--output-report <path-to-sanitized-json>`

The report is sanitized and includes WSDL reachability, authentication, incoming invoice listing, records inspected, detail/UBL retrieval, document persistence, SHA-256 verification, collected representative scenarios, missing scenarios, planned read-only operations, successfully validated read-only operations, configuration failures, permission failures, parser-validation blockers, and `no_provider_state_change_attempted=true`.

Safety boundaries:

- production Uyumsoft is forbidden for this validation
- the script fails before any provider call unless the configured test WSDL host is `efatura-test.uyumsoft.com.tr`
- only `TestConnection`, `WhoAmI`, `GetInboxInvoiceList`, and `GetInboxInvoiceData` are used
- acknowledgement, accept/reject, cancel, status update, mark-as-read, send, retry, and move-to-draft operations are not attempted
- credentials, SOAP envelopes, raw SOAP responses, full UBL/XML contents, full invoice contents, and sensitive company/person invoice data are omitted

Troubleshooting:

- `configuration_failures`: fix the named environment variable and never substitute production Uyumsoft values
- authentication or permission failure: verify the test account has listing and invoice-data read permission only
- empty listing: widen `--from-date`/`--to-date` or confirm the Uyumsoft test inbox contains documents
- missing dataset scenarios: record the missing scenario names from the sanitized report; do not fabricate fixtures
- persistence/hash failure: inspect database connectivity and `DOCUMENT_STORAGE_ROOT` writability

Downloaded UBL files live under `DOCUMENT_STORAGE_ROOT` and must not be committed. Record collected dataset evidence by storing only the sanitized report or internal document ids/hashes in validation notes.

## Logging And Redaction

Structured logs may include safe correlation fields:

- operation status
- duration
- integration invoice id
- ETTN/UUID
- sync run id
- document id
- Odoo move id
- safe error category

Never log:

- passwords
- API keys
- tokens
- full database URLs
- full XML
- SOAP payloads
- full Odoo payloads
- sensitive invoice content

The runtime logging filter redacts common secret key/value patterns and XML-like payload strings. Services must still avoid sending full payloads to logging APIs.

## Timeout And Retry Policy

- Uyumsoft list and document calls use bounded retries for transient transport failures only.
- Uyumsoft SOAP faults, validation failures, and authorization failures are not retried as transient transport errors.
- Odoo JSON-2 calls use bounded HTTP client timeouts and do not run implicit infinite retries.
- Odoo draft creation relies on ETTN idempotency before any retry or repeated operation.
- No unsafe write operation is retried without an idempotency guarantee.

## Deployment Runbook

1. Confirm CI is green for the exact commit to deploy.
2. Confirm no secrets are committed and real environment profile files remain untracked.
3. Review the go-live checklist below and record approvals.
4. Back up PostgreSQL and document storage.
5. Build and tag the application image from the approved commit.
6. Run `ruff check .`, `ruff format --check .`, and `pytest`.
7. Run migration validation in staging: `alembic upgrade head`.
8. Start the application with production env values from the secret store.
9. Verify startup succeeds; unsafe production config must fail fast.
10. Verify `GET /health/live` and `GET /health/ready`.
11. Run explicit read-only provider smoke checks in the approved environment.
12. Observe API and DB logs for startup errors, retry spikes, and redaction.
13. Keep finance/business owner available during first controlled sync.

## Migration And Rollback

Before migration:

```bash
docker compose exec db pg_dump -U <db-user> -d <db-name> -Fc -f /tmp/pre_migration.dump
alembic current
alembic upgrade head
```

Rollback decision process:

- If application startup fails before traffic, roll back the image first.
- If migration changed schema and no production writes occurred, consider `alembic downgrade -1` after confirming the target revision.
- If production writes occurred, prefer restoring from backup or applying a forward fix after owner approval.
- Database rollback does not remove Odoo draft records that were already created.

Application rollback:

1. Stop traffic to the failing version.
2. Deploy the previous approved image.
3. Verify `/health/live` and `/health/ready`.
4. Inspect logs for repeated connector or database errors.
5. Reconcile any in-flight sync/document/draft operation by ETTN and operation id.

## Backup And Restore Checklist

- PostgreSQL backup captured and restore-tested.
- Document storage root backed up with hashes preserved.
- Runtime configuration backed up through the secret manager without exporting secret values to logs.
- Alembic revision before and after migration recorded.
- Restore verification includes database connectivity, document storage readability, and representative metadata lookup.

Docker-compatible examples:

```bash
docker compose exec db pg_dump -U <db-user> -d <db-name> -Fc -f /tmp/ict_backup.dump
docker compose cp db:/tmp/ict_backup.dump ./backups/ict_backup.dump
docker compose exec db pg_restore --list /tmp/ict_backup.dump
```

Use environment-specific secure storage for backups. Do not commit backups.

## Incident Response Runbook

- Provider unavailable: pause manual sync/download actions, verify retries are bounded, notify provider owner, resume only after read-only smoke succeeds.
- Odoo unavailable: pause resolution and draft creation, preserve local sync/document state, retry only after Odoo health is restored.
- Database unavailable: stop write workflows, restore DB connectivity, verify Alembic current, inspect failed operation records.
- Storage unavailable: stop document downloads and parsing, restore storage mount, verify hash/read access.
- Duplicate draft concern: search local `odoo_draft_invoices` by ETTN, then manually inspect Odoo draft bills by ETTN/reference; do not unlink automatically.
- Partial Odoo creation/local persistence failure: use the reconciliation steps below.
- Credential exposure suspicion: rotate affected credentials, revoke tokens, inspect logs/backups, confirm no real environment profile file was committed.
- Invalid production configuration: keep service stopped, fix secret-store values, rerun readiness after startup.
- Excessive failures or retries: disable manual triggering, inspect safe error categories, and escalate to technical owner.

## Recovery And Reconciliation

Known risk: Odoo draft is created successfully but local persistence fails afterward.

Safe manual procedure:

1. Find the invoice ETTN/UUID from the approved source record.
2. Search Odoo draft vendor bills by reference/ETTN and confirm `state=draft`.
3. Confirm no duplicate draft exists for the same ETTN.
4. If one draft exists, record the Odoo move id in the incident notes.
5. Restore or repair local persistence with owner approval.
6. Re-run the idempotent draft workflow only after local state is consistent.
7. Never post, unlink, cancel, or mutate provider state as part of reconciliation.

## Production Permissions

### Uyumsoft

Minimum permissions:

- invoice listing
- invoice detail retrieval
- UBL document download

Not required by default:

- status-changing permissions
- acknowledgement or receipt permissions
- acceptance, rejection, cancellation, retry-send, move-to-draft, or send permissions

### Odoo

Minimum permissions:

- read partner, product, tax, currency, and journal records for resolution
- create draft vendor bills only when explicitly approved

The integration user does not need invoice posting permission. Do not grant unlink. Do not grant partner, product, or tax master-data creation unless separately approved.

## Go-Live Checklist

Production must remain disabled until every mandatory gate is explicitly approved.

- CI green
- local tests green
- Docker build/start validated
- migrations validated
- backup completed
- restore procedure reviewed
- production endpoints approved
- credentials stored securely
- least-privilege permissions reviewed
- logging redaction verified
- no secrets committed
- staging/test end-to-end validation completed
- idempotency validated
- manual reconciliation procedure reviewed
- Odoo draft-only behavior verified
- `action_post` prohibition verified
- Uyumsoft read-only behavior verified
- finance/business owner approval recorded
- technical owner approval recorded
- rollback owner identified
- monitoring owner identified

## Monitoring Expectations

Track these signals with the deployment platform already in use:

- API process health and restart count
- `/health/ready` status
- database connectivity and migration revision
- sync run failures and retry counts
- document download conflicts/failures
- parser failures by safe category
- Odoo resolution missing/ambiguous counts
- Odoo draft creation failures and duplicate-in-progress counts

Do not add production traffic until owners agree how these signals are observed and escalated.
