# Integration Flow

This flow describes the controlled production path. Each stage keeps provider-specific behavior inside connectors or services and avoids automatic posting or destructive operations.

```text
Uyumsoft
  -> Incremental Sync
  -> Document Download
  -> UBL Parser
  -> Mapping Preview
  -> Odoo Resolution Engine
  -> Draft Vendor Bill Creation
```

## Uyumsoft

- Input: runtime WSDL endpoint, WS-Security username/password, bounded read request.
- Output: provider SOAP responses.
- Responsibility: provider communication only.
- Allowed operations: `TestConnection`, `WhoAmI`, `GetSystemDate`, `GetInboxInvoiceList`, `GetOutboxInvoiceList`, `GetInboxInvoiceData`, `GetOutboxInvoiceData`.
- Forbidden operations: `SendInvoice`, `SetInvoicesTaken`, `Cancel*`, `RetrySendInvoices`, `MoveToDraftStatus`, acknowledgement, acceptance, rejection, cancellation, status mutation.
- Failure behavior: connector raises safe domain connector errors; provider payloads and credentials are not logged.

## Incremental Sync

- Input: bounded date window, direction, page size, maximum pages.
- Output: normalized invoice metadata and sync run summary.
- Responsibility: call read-only listing, track cursor/page progress, persist metadata idempotently.
- Allowed operations: inbox/outbox invoice listing only.
- Forbidden operations: UBL/PDF download, invoice acknowledgement, status mutation, Odoo writes.
- Failure behavior: partial runs are marked failed with safe error details; already persisted metadata remains idempotent by ETTN/fallback identity.

## Document Download

- Input: existing invoice metadata ids and explicit read-only confirmation.
- Output: local `UBL_XML` file plus document metadata row.
- Responsibility: decide whether a document should be downloaded, calculate SHA-256, store file content outside PostgreSQL, persist metadata.
- Allowed operations: `GetInboxInvoiceData`, `GetOutboxInvoiceData` for UBL XML only.
- Forbidden operations: PDF/XSLT/ZIP download, status mutation, acknowledgement, Odoo writes.
- Failure behavior: storage or connector failures return safe errors; XML content, SOAP payloads, and credentials are not logged.

## UBL Parser

- Input: stored `UBL_XML` document from the document layer.
- Output: provider-independent normalized invoice model.
- Responsibility: local-only XML parsing and validation.
- Allowed operations: local file read through document storage.
- Forbidden operations: Uyumsoft network calls, Odoo calls, external entity resolution, XML persistence to logs.
- Failure behavior: structured safe parser errors identify category and field/path without exposing full XML.

## Mapping Preview

- Input: normalized invoice model.
- Output: Odoo-compatible draft invoice payload preview with warnings and missing fields.
- Responsibility: provider-independent deterministic transformation.
- Allowed operations: local model mapping only.
- Forbidden operations: Odoo API calls, persistence, partner/product/tax creation.
- Failure behavior: preview returns `needs_review` with missing fields and warnings.

## Odoo Resolution Engine

- Input: mapping preview.
- Output: reviewed preview with existing Odoo ids and structured resolution results.
- Responsibility: read-only lookup for partner, product, tax, currency, and purchase journal.
- Allowed operations: Odoo JSON-2 `search_read` on allowlisted models only.
- Forbidden operations: create, write, unlink, archive, `action_post`, master-data mutation, draft creation.
- Failure behavior: missing or ambiguous candidates are returned as reviewable statuses; no ambiguous record is auto-selected.

## Draft Vendor Bill Creation

- Input: reviewed mapping preview with required Odoo ids, ETTN, and explicit confirmation.
- Output: draft `account.move` id and local idempotency record.
- Responsibility: create only draft vendor bills and persist the Odoo reference idempotently.
- Allowed operations: Odoo JSON-2 `account.move/create` for `move_type=in_invoice`.
- Forbidden operations: `action_post`, existing invoice update, unlink, partner/product/tax creation, payment registration, reconciliation.
- Failure behavior: ETTN prevents duplicate drafts; connector failures are recorded safely. If Odoo draft creation succeeds but local persistence fails, use the reconciliation runbook in `docs/PRODUCTION_READINESS.md`.
