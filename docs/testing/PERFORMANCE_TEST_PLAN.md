# Performance Test Plan

This plan defines practical measurement goals for the current architecture. It does not establish production SLA commitments.

## Objectives

- Measure parser, mapping, resolution, draft creation, and total processing duration.
- Confirm bounded configuration remains enforced under load.
- Identify bottlenecks before production use.
- Verify logs remain aggregate and safe under volume.

## Test Volumes

Use synthetic or approved non-production data sets:

| Volume | Purpose |
| --- | --- |
| 100 invoices | baseline functional throughput and timing |
| 500 invoices | moderate batch behavior and database/storage pressure |
| 1000 invoices | upper planning exercise for current manual/bounded workflows |

The incremental sync API currently enforces bounded date ranges, page size, and max pages. Do not bypass those bounds to create unrealistic load.

## Measurements

Record:

- parser duration per document and per batch
- mapping duration per invoice and per batch
- Odoo resolution duration per invoice and per batch
- draft creation duration per invoice and per batch when explicitly approved in non-production
- total processing time
- average latency
- p50/p95 latency when practical
- throughput in invoices per minute
- database insert/update/skip counts
- document storage write/read duration
- error count by safe category

## Acceptable Targets

Do not invent hard SLA values before production traffic is measured. Initial targets should be comparative and operational:

- 100 invoice batch completes without errors on a developer or staging environment.
- 500 invoice batch completes within an agreed maintenance/test window.
- 1000 invoice batch produces stable measurements without unbounded memory growth, unbounded retries, duplicate rows, or unsafe logs.
- Any target that affects finance operations must be approved during UAT or production readiness.

## Method

1. Prepare synthetic UBL XML fixtures with representative invoice structures.
2. Load existing invoice metadata and document metadata as needed.
3. Run parser validation and collect durations.
4. Run mapping preview validation and collect durations.
5. Run Odoo resolution with mocks or approved non-production Odoo data and collect durations.
6. Run draft creation only in an approved non-production environment with explicit confirmation.
7. Inspect logs for redaction and aggregate-only behavior.
8. Record hardware/container limits and database/storage configuration with the result.

## Reporting Template

| Run ID | Environment | Volume | Parser total | Mapping total | Resolution total | Draft total | End-to-end total | Throughput | Errors | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  |  | 100 / 500 / 1000 |  |  |  |  |  |  |  |  |

## Guardrails

- Do not call Uyumsoft or Odoo production systems.
- Do not download PDF, XSLT, ZIP, or unsupported document types.
- Do not call Uyumsoft status-changing operations.
- Do not call Odoo `action_post`, `write`, `unlink`, or master-data creation operations.
- Do not log full XML, SOAP payloads, full Odoo payloads, credentials, or secrets.
