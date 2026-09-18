# ONE_OFF_VENDOR retirement status and recovery (P0-PROD-09E)

`GET /api/workbench/reviews/{review_id}/one-off-vendor-retirement` requires
`workbench_review_read`. It returns only persisted Hub retirement identity,
company, historical resolution version, partner ID and lifecycle status. It never
contacts Odoo or changes state. Optional `review_version` selects an exact row;
omitting it selects the latest retirement row. This version can differ from the
current review/decision version. Missing review/retirement within the authenticated
company returns 404; an unrelated tenant's identity is never exposed.

`POST .../one-off-vendor-retirement/recover` requires `workbench_execute` and body
`{"review_version": 2}` using the version returned by GET. Company and named
approver come from authenticated RequestContext. Caller-supplied partner, company,
approver, authorization or ERP payload fields are rejected. The workflow validates
the company-scoped review and exact retirement row before composing ERP adapters,
then invokes the existing `ArchiveOneOffVendorUseCase` once.

Existing behavior is preserved:

- `pending_vendor_bill` without successful durable Vendor Bill evidence returns
  `awaiting_vendor_bill` without contacting Odoo.
- `archived` is idempotent terminal success with `already_applied=true`; no Odoo
  read or write occurs.
- `archive_attempted` and `needs_reconciliation` use the existing archived-inclusive
  partner read-back first. Confirmed inactive means no new archive write. Failed
  read-back fails safely, leaving reconciliation visible through GET.
- An active partner can be archived only through existing
  `SUPPLIER_REMEDIATION_WRITE_ENABLED`, master `PRODUCTION_OPERATIONS_ENABLED`,
  production ACK and named-approver checks. Closed gates forbid writes; the writer
  may still confirm an already-inactive partner without a write.
- Transport uncertainty remains an error; existing lifecycle persistence records
  `needs_reconciliation`. No automatic retry or blind archive is added. Operators
  inspect GET and explicitly request recovery; the same writer again reads back
  before deciding whether any write is necessary.

Recovery does not execute/retry a Vendor Bill, create a partner, authorize supplier
or product operations, reclassify, modify accounting, or publish an Odoo projection.
There is no migration, new dependency, archive logic or lifecycle/retry change.
Rollback consists of reverting this API/application wiring; existing persisted rows
and remote effects remain unchanged. Tests use local databases and mocked ERP.
