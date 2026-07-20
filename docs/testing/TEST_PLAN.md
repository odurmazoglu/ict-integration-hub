# Test Plan

This plan documents the complete validation process for ICT Integration Hub. Use mocked automated tests for routine validation and controlled test environments for UAT or go-live exercises.

## Phase 1: Parser Validation

- Objective: confirm stored `UBL_XML` documents parse into provider-independent normalized invoice models.
- Prerequisites: local storage contains synthetic or approved test UBL XML; parser tests are available; XML fixtures contain no real secrets.
- Input: stored `UBL_XML` document metadata and local XML bytes.
- Expected output: normalized invoice header, parties, monetary totals, tax totals, tax subtotals, and invoice lines.
- Success criteria: valid invoices parse with `Decimal` monetary values and timezone-aware dates where possible; malformed XML, unsupported document type, missing required fields, invalid dates, and invalid decimals produce safe structured parser errors without full XML in logs or exceptions.

## Phase 2: Mapping Validation

- Objective: confirm normalized invoice models produce Odoo-compatible draft invoice payload previews without calling Odoo.
- Prerequisites: representative normalized invoice fixtures; mapping preview service available.
- Input: normalized invoice model from parser or synthetic fixture.
- Expected output: preview object with invoice payload, line payloads, warnings, missing fields, and mapping status.
- Success criteria: valid invoices map to `ready`; missing partner, currency, tax, quantity, price, invoice number, or ETTN are reported as warnings or missing fields; no Odoo API call or persistence occurs.

## Phase 3: Resolution Validation

- Objective: confirm Odoo Resolution Engine resolves existing Odoo ids deterministically using read-only calls.
- Prerequisites: mocked Odoo JSON-2 client or approved non-production Odoo environment; mapping preview payload exists.
- Input: mapping preview payload.
- Expected output: reviewed preview with resolved, unresolved, ambiguous, invalid, or not-required statuses for partner, product, tax, currency, and journal.
- Success criteria: exact VAT/name/product/tax/currency/journal matching works; ambiguous records are not auto-selected; missing records remain reviewable; no create, write, unlink, archive, or `action_post` calls occur.

## Phase 4: Draft Vendor Bill Validation

- Objective: confirm reviewed payloads create only draft vendor bills when explicitly approved.
- Prerequisites: reviewed mapping preview with required Odoo ids; draft creation confirmation enabled; production is disabled unless production approval gates are complete.
- Input: reviewed preview with `mapping_status=ready`, no missing fields, ETTN, partner id, product ids, tax ids, currency id, and journal id.
- Expected output: draft Odoo `account.move` reference and local idempotency record.
- Success criteria: only `account.move/create` for draft vendor bill is used; `action_post` is never called; repeated ETTN returns existing local reference; failed attempts are recorded safely.

## Phase 5: Integration Validation

- Objective: validate the controlled flow from Uyumsoft metadata sync through draft payload preparation using approved test data.
- Prerequisites: test-environment configuration; safe credentials stored outside the repository; bounded date range; explicit confirmations; local database and document storage available.
- Input: narrow test date window, direction, page size, max pages, selected invoice ids, and UBL XML documents.
- Expected output: sync run summary, invoice metadata rows, document metadata rows, parsed normalized invoices, mapping previews, resolution results, and optional draft vendor bill records when approved.
- Success criteria: all provider calls stay within allowed operations; pagination is bounded; ETTN/fallback idempotency prevents duplicates; logs contain safe aggregate data only.

## Phase 6: Failure Injection

- Objective: prove common failures produce safe errors and recoverable states.
- Prerequisites: local or staging environment where services can be stopped, credentials can be replaced with placeholders, and failures can be simulated.
- Input: controlled outages, invalid configuration, malformed XML, provider timeouts, database/storage failures, and repeated retry attempts.
- Expected output: safe error categories, failed run records where applicable, no secret leakage, no unbounded retry loop, and clear recovery path.
- Success criteria: application fails fast for unsafe production config; retries are bounded; partial persistence remains idempotent; no provider or Odoo state-changing operation is called.

## Phase 7: Performance Validation

- Objective: measure processing behavior for realistic invoice volumes without inventing hard SLA guarantees.
- Prerequisites: synthetic fixture sets for 100, 500, and 1000 invoices; local or staging database; timing collection method.
- Input: batches of normalized invoice metadata, UBL XML documents, mapping previews, and resolution fixtures.
- Expected output: parser, mapping, resolution, draft creation, total duration, average latency, and throughput measurements.
- Success criteria: results are recorded, bounded configuration remains enforced, resource usage is acceptable for the deployment environment, and bottlenecks are identified before production.

## Phase 8: User Acceptance Testing

- Objective: confirm accounting users can verify draft vendor bills before posting in Odoo.
- Prerequisites: UAT environment, finance users, representative test invoices, and controlled draft-only Odoo access.
- Input: draft vendor bills and source invoice metadata/XML references.
- Expected output: finance sign-off or documented corrections.
- Success criteria: supplier, invoice number, date, currency, taxes, totals, journal, UUID, XML attachment availability, and duplicate prevention are verified.

## Phase 9: Production Go-Live Validation

- Objective: confirm production is safe to enable.
- Prerequisites: CI green, local tests green, UAT approved, backups verified, rollback plan reviewed, secrets configured, production approval completed.
- Input: final approved commit, production configuration, go-live checklist, and owner approvals.
- Expected output: Go / No-Go decision.
- Success criteria: all mandatory gates pass, owners sign off, rollback path is ready, and production remains disabled until approval is explicit.
