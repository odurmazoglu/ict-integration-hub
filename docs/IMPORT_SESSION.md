# Import Session

Import Session is the orchestration and future durable audit unit for import attempts in ICT IPP.

The current repository implements an in-memory `ImportSession` application orchestrator for sequential multi-invoice imports. It does not persist sessions yet.

The repository also already tracks sync runs, invoice metadata, document metadata, and draft invoice references. A consolidated persistent Import Session model remains an accepted future architecture decision, not yet implemented as a single table.

## Current Implementation

`ImportSession` lives in the Application layer and coordinates multiple `InternalInvoice` DTOs by delegating every invoice to `ImportInvoiceUseCase`.

```mermaid
flowchart TB
    InvoiceList[Invoice List]
    ImportSession[ImportSession]
    ImportInvoiceUseCase[ImportInvoiceUseCase]
    VendorBillWriter[VendorBillWriter]
    Odoo[Odoo]

    InvoiceList --> ImportSession
    ImportSession --> ImportInvoiceUseCase
    ImportInvoiceUseCase --> VendorBillWriter
    VendorBillWriter --> Odoo
```

Current responsibilities:

- execute imports sequentially
- collect `ImportInvoiceResult` values
- measure started, finished, and elapsed duration
- count processed, successful, duplicate, review-required, and failed invoices
- continue processing remaining invoices after one invoice fails
- return immutable `ImportSessionResult`

Current boundaries:

- no persistence
- no parallel processing
- no retry loop
- no scheduler
- no Rule Engine
- no Decision Engine
- no AI Advisor
- no Company Memory
- no matching, workflow-selection, review-reason creation, Vendor Bill creation, ERP, HTTP, SOAP, or SQL logic

Review-required invoices are collected separately from failed invoices. A Manual Review result means deterministic business data needs human review; a failure means an application or dependency error prevented the invoice workflow from completing normally.

## Purpose

The current in-memory Import Session groups the result of one sequential multi-invoice import run.

The future durable Import Session should group source data, rule execution, workflow decisions, AI recommendations, user review, ERP execution, and traceability outcomes.

It should answer:

- What was imported?
- Which deterministic rules ran?
- Which workflow and strategy were selected?
- What did AI recommend, if anything?
- Who reviewed unresolved items?
- What ERP action was executed?
- Which procurement traceability links were preserved?
- What failed and how can it be retried safely?

## Import Session Flow

```mermaid
flowchart TB
    Created[Session created] --> Ingested[Source metadata ingested]
    Ingested --> Documents[Documents acquired]
    Documents --> Parsed[Documents parsed]
    Parsed --> Rules[Rule Engine evaluated]
    Rules --> Decision[Decision Engine selected workflow and strategy]
    Decision --> AI[AI Advisor recommendation recorded]
    Decision --> Review{Review required?}
    Review -->|yes| UserReview[User review]
    Review -->|no| Execute[ERP execution]
    UserReview --> Execute
    Execute --> Completed[Completed or failed with safe state]
```

## Expected Data

Future Import Session records should include:

- session id
- source provider
- source document identifiers
- ETTN/UUID
- import type
- workflow id
- selected strategy id
- rule result references
- AI recommendation references
- company memory references used
- review status
- reviewed user/action metadata
- ERP execution references
- traceability chain references
- safe failure category and message
- created, updated, started, finished timestamps

## Status Model

Current in-memory statuses:

- CREATED
- RUNNING
- COMPLETED
- FAILED
- CANCELLED

`CANCELLED` is reserved for future explicit cancellation behavior and is not currently produced by the sequential executor.

Recommended future durable statuses:

- created
- ingesting
- parsed
- rules_evaluated
- needs_review
- ready_for_execution
- executing
- completed
- failed
- blocked
- cancelled_by_user

Do not collapse ambiguous, missing, invalid, and blocked states into a single generic failure if downstream users need different actions.

## Relationship To Current Tables

Current tables represent parts of the future Import Session:

- `uyumsoft_invoice_metadata`: source invoice metadata and identity.
- `uyumsoft_sync_runs`: bounded sync execution summary.
- `invoice_documents`: acquired UBL XML document metadata.
- `odoo_draft_invoices`: draft creation idempotency and Odoo reference.

A future Import Session may reference these records rather than replacing them.

## Idempotency

ETTN/UUID remains the primary identity for invoice import and draft creation. Import Session identity should preserve ETTN and link to source provider identity. Missing ETTN may use documented fallback identity for metadata ingestion, but ERP draft creation requires ETTN.

## Audit And Privacy

Import Session records must support review without storing raw credentials, SOAP envelopes, full XML, or full ERP payloads in logs. Store sensitive documents only through approved document storage boundaries.

## Related Documents

- [Import Workbench](IMPORT_WORKBENCH.md)
- [Company Memory](COMPANY_MEMORY.md)
- [Import Session ADR](adr/ADR-0008-import-session.md)
