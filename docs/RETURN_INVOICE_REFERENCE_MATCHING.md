# Return-Invoice Reference Matching (PR R)

Operator-triggered, read-only lookup that links an incoming return invoice
(`InvoiceTypeCode` IADE family) to the original outgoing invoice it reverses, using the
company's Uyumsoft outbox. The result is **informational only**.

## Safety boundaries

- Nothing is persisted: no table, no migration, no evidence change, no review/decision
  change. The use case has no writer, unit-of-work, decision or projection dependency.
- Uyumsoft is called only by the explicit action
  `POST /api/workbench/reviews/{review_id}/return-reference-lookup`. Workbench queue and
  detail reads never build the lookup or a Uyumsoft client.
- Only the allowlisted read operation `GetOutboxInvoiceList` is used. No sync, no import,
  no outbox metadata persistence, no state-changing Uyumsoft operation.
- PR S guards are unchanged: a return invoice still carries `RETURN_INVOICE_NOT_SUPPORTED`
  and can never become a Vendor Bill, whatever the lookup returns. No credit note is created.
- Permissions: `workbench_review_read` **and** `invoice_document_read` (provider access).
  Company scope comes from `RequestContext`.

## Reference extraction

The stored raw UBL document of the review (linked through the review's import
idempotency key, verified by SHA-256 and by `cbc:UUID`) is re-read at request time.
Every `Invoice/cac:BillingReference/cac:InvoiceDocumentReference` entry is extracted in
document order (`cbc:ID`, `cbc:IssueDate`, `cbc:DocumentTypeCode`).

`InternalInvoice` and the persisted source-invoice evidence are deliberately **not**
extended: evidence is fingerprinted on replay, so a new serialized field would turn every
replay of an invoice carrying a BillingReference into an idempotency conflict.

Free text (`cbc:Note`, line names/descriptions) and `AdditionalDocumentReference` are
never used as references.

## Provider field semantics (verified against production)

| Uyumsoft `OutboxInvoiceListItem` | Meaning |
| --- | --- |
| `InvoiceId` | Official GİB invoice number |
| `DocumentId` | ETTN |
| `InvoiceTipType` | Invoice type (e.g. `Sales`) |
| `Type` | Scenario (e.g. `ComercialInvoice`) |
| `TargetTcknVkn` | Original buyer tax number |
| `ExecutionDate` | Issue date (naive values are Turkish local time) |

`UyumsoftOutboxInvoiceRecord` maps these explicitly; the generic list-summary mapping
keeps its historical aliases unchanged.

## Search

Per valid reference (16-character GİB number and a valid `IssueDate`), two exact-number
searches run: `IsArchived=false` and `IsArchived=true` (the provider's default excludes
archived invoices). Each uses `InvoiceNumbers=[number]`, a window of `IssueDate ± 1` local
day, page 1 and page size 10. A requested filter missing from the WSDL fails closed, so an
exact lookup can never degrade into a date-window listing. Duplicate references are
searched once.

## Deterministic rules

- Only a candidate whose `InvoiceId` equals the reference exactly counts. No fuzzy
  matching; no candidate is ever chosen automatically.
- Either search failing ⇒ `INCOMPLETE_SEARCH` (never `NOT_FOUND`, never `MATCHED`).
- More than one exact candidate, or a provider total larger than the page ⇒ `AMBIGUOUS`.
- No exact candidate in both completed searches ⇒ `NOT_FOUND`.
- One candidate ⇒ `MATCHED` only if every check passes, else `MISMATCH`:

| Check | Rule |
| --- | --- |
| `COMPANY_IDENTITY` | exactly one Odoo company has the return buyer tax number, and it is the review company |
| `SELLER_IDENTITY` | the original comes from that company's own outbox and `COMPANY_IDENTITY` holds (the outbox item has no seller tax number) |
| `BUYER_IDENTITY` | original `TargetTcknVkn` equals the return seller tax number (digits only) |
| `CURRENCY` | original currency equals the return currency |
| `ORIGINAL_DATE` | original issue date equals the reference `IssueDate` and is not after the return |
| `INVOICE_TYPE` | IADE ⇒ `Sales`; other return variants are not yet verified and fail this check |
| `INVOICE_STATUS` | current provider status is `Approved` |

Reference problems are `REFERENCE_UNPARSEABLE` (missing/invalid number or date, malformed
XML, missing/ambiguous/unreadable/altered source document) and are never searched. No
reference at all ⇒ `REFERENCE_MISSING`. The overall status is `MATCHED` only when every
reference matched; otherwise the first present of `INCOMPLETE_SEARCH`,
`REFERENCE_UNPARSEABLE`, `AMBIGUOUS`, `MISMATCH`, `NOT_FOUND`.

An unavailable company-identity read is an error (HTTP 503), not a conclusion.

## Known provider paging anomaly (not changed here)

During the production probe a request with `PageIndex=1` returned the single match and
echoed `PageIndex=0`; with no results it echoed `PageIndex=1`. One hypothesis is that the
provider index is 0-based and out-of-range pages are clamped to the last page. If that
holds, the shared sync's 1-based paging returns the right rows only while a window has at
most `page_size` invoices; with more, page 0 would never be requested. Current inbound
volume stays far below 100 invoices per poll window. This is **not verified** and the
shared pagination is unchanged; it needs a separate review with a test-environment check
(for example page size 1 over a window holding at least two invoices, comparing
`PageIndex` 0 and 1). The PR R lookup is not affected in practice: an exact-number
search returns at most a handful of rows, and a truncated page is reported as
`AMBIGUOUS`.

## Rollback

Code-only change: revert the commit. No migration, no stored data, no flag.
