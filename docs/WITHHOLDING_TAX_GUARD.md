# Withholding-Tax / Source-Document Tax Guard (PR T W1)

A Vendor Bill is never created for an incoming invoice whose original UBL-TR document
declares tax withholding (tevkifat), carries a tax other than VAT, or cannot be proven to be
the document the review was imported from. The guard is fail-closed and read-only.

W0 (#237, deployed, rollback floor `config-api:rollback-w0-floor-f7c3ab2c`) added the reason
codes for rollback compatibility; W1 emits them.

## What is checked

Facts are extracted from the **original stored UBL document** (`app/domain/invoice/tax_facts.py`),
never from `InternalInvoice` -- the canonical parser ignores `TaxTypeCode` and
`WithholdingTaxTotal`, and persisted source evidence is fingerprinted on replay, so it is not
changed.

| Fact | Blocks with |
| --- | --- |
| `InvoiceTypeCode` `TEVKIFAT` / `YTBTEVKIFAT` (and the `*IADE` variants) | `WITHHOLDING_TAX_NOT_SUPPORTED` |
| any `WithholdingTaxTotal` -- invoice-level (child of `Invoice`) or line-level, any namespace | `WITHHOLDING_TAX_NOT_SUPPORTED` |
| a `TaxSubtotal` (outside a withholding total) whose `TaxTypeCode` is not `0015`, or has none | `UNSUPPORTED_INVOICE_CONTENT` (`source=source_tax`) |
| document missing / ambiguous / SHA-256 mismatch / unsafe or malformed XML / UUID missing or mismatched | `SOURCE_DOCUMENT_UNVERIFIED` (`details.issue`) |

Verification before any fact is trusted (`app/application/source_tax/assessment.py`):

1. the review's document is located with PR R's locator (import idempotency key -> provider
   metadata -> `invoice_documents`); zero or several UBL rows are unverified;
2. `sha256(bytes)` must equal `invoice_documents.content_hash_sha256`;
3. the XML safety rules are the canonical parser's (no DTD / entities, UBL `Invoice` root);
4. the document `cbc:UUID` must equal the review's source-invoice UUID exactly.

Storage failures are classified (`LocalDocumentStorage` + `StoredSourceTaxDocumentReader`):

| Condition | Result |
| --- | --- |
| unsafe / invalid storage key (empty, `.`, absolute, `..`, NUL, escapes the root incl. via symlink) | `SOURCE_DOCUMENT_UNVERIFIED` (`STORAGE_KEY_INVALID`) |
| file missing while the root is a readable, populated directory | `SOURCE_DOCUMENT_UNVERIFIED` (`DOCUMENT_FILE_MISSING`) |
| root missing, not a directory, unreadable, or **empty** (an unmounted volume leaves an empty mountpoint) | retryable `SourceTaxFactsUnavailableError` (HTTP 503) |
| permission denied, I/O error, stale handle, any other `OSError`; database failure | retryable `SourceTaxFactsUnavailableError` (HTTP 503) |

A retryable failure is never a verdict and persists nothing. A persisted
`SOURCE_DOCUMENT_UNVERIFIED` is not permanent either: a later reclassification recomputes it
from the stored state.

## Where it is enforced

| Path | Mechanism |
| --- | --- |
| Canonical import (poller / sync) | facts computed from the bytes just stored, hash checked against the download record; passed as `ImportInvoiceCommand.source_tax_assessment` |
| Reclassification, evidence recovery, source-identity correction | `EffectiveDecisionResolver` (required `source_tax_facts_reader`) reads the stored document for every recomputation |
| Rule engine | source-tax reasons prepended after the PR S return guard; no Vendor Bill branch while any is present. A non-dry-run evaluation without an assessment is `SOURCE_DOCUMENT_UNVERIFIED` (`FACTS_NOT_SUPPLIED`) |
| Stage-1 evidence | never built for a source-tax hard block or any `source=source_tax` reason |
| Decision acceptance | `SubmitReviewDecisionUseCase` re-verifies for every VENDOR_BILL decision (replays too); refused without the check |
| Vendor Bill preview | required check before the build |
| Vendor Bill execution, Existing Purchase Order billing, Customer Invoice (recharge) creation | required check on **every attempt** (DRY_RUN / EXECUTE, retry, resume), before the RESALE check, the build and any Odoo call; every accounting mode passes through it. The customer-invoice check is independent of the Vendor Bill step and of persisted billing evidence |

The execution-side checks do not trust the review's persisted reasons or Stage-1/2 evidence,
so evidence pinned before W1, operator tax / account / product resolutions and review-scoped
accounting decisions cannot route around them. Source-tax reasons are never stripped by the
effective-decision resolver. PR S precedence is unchanged: a return invoice carries only
`RETURN_INVOICE_NOT_SUPPORTED`.

HTTP mapping (`app/api/routers/workbench.py`): `SourceTaxSafetyBlockedError` -> 409,
`SourceTaxFactsUnavailableError` -> 503. Execution step error codes:
`source_tax_safety_blocked`, `source_tax_facts_unavailable`.

## Compatibility

- No migration, no setting, no Odoo / Studio / Keycloak change. Alembic head stays `202607170042`.
- Source-invoice evidence and its fingerprint are unchanged (facts live only on the command).
- Today's production invoices (VAT `0015` only, one tax per line, no withholding) produce no
  source-tax reason: existing reviews only change if reclassified, and a reclassification of a
  clean document is a no-op.
- The legacy `/odoo-mapping/draft-invoices` route (404 in production) is not covered.

## Rollback runbook

Pre-W1 rollback target is the W0 floor; **never roll back below it once W1 has run.**

1. Before deploying W1: tag the running W0 image (already `config-api:rollback-w0-floor-f7c3ab2c`)
   and take a database backup.
2. To roll W1 back: `git checkout --detach 221a542` in the app build context, then
   `docker tag config-api:rollback-w0-floor-f7c3ab2c config-api:latest` and
   `docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate api uyumsoft-inbound-poller`.
   No database or environment restore is needed (W1 adds no migration and no setting).
3. Verify: `/health`, both containers on the W0 image, Workbench detail reads succeed for
   reviews carrying the new codes (W0 can read and display them).
4. Known W0 limitation after a rollback: W0 cannot detect withholding, so a reclassification or
   source-identity correction run on W0 would drop a W1 code, and W0 has no execution backstop.
   While on W0, do not reclassify, correct, decide or execute reviews that carry
   `WITHHOLDING_TAX_NOT_SUPPORTED` / `SOURCE_DOCUMENT_UNVERIFIED`; re-deploy W1 instead.
5. Never roll back to an image older than W0 (`rollback-prt-w0-pre-190328d7` or earlier):
   those images cannot parse the new reason codes (`ManualReviewReasonCode` / `REASON_TITLES`).

Count stored rows carrying the codes (read-only) before deciding:

```sql
select count(*) from workbench_review_items
where review_reasons::text ~ '(WITHHOLDING_TAX_NOT_SUPPORTED|SOURCE_DOCUMENT_UNVERIFIED)';
```
