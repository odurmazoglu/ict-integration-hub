# Roadmap

The roadmap keeps ICT Integration Hub production-safe while growing ICT IPP architecture in small reviewed slices.

## Delivery Principles

- Ship small production-ready PRs.
- Keep documentation and ADRs aligned with behavior.
- Do not redesign accepted architecture as part of feature work.
- Add tests for every implementation change.
- Keep provider and ERP writes explicitly approved and narrowly scoped.
- Preserve procurement traceability whenever possible.

## Current Foundation

The current repository already supports:

- FastAPI service and Docker runtime
- selected environment profile loading
- production safety gates
- Uyumsoft read-only invoice listing and UBL XML retrieval
- idempotent metadata and document persistence
- local UBL parsing
- Odoo mapping preview
- read-only Odoo resolution
- draft-only Odoo vendor bill creation after explicit confirmation
- ERP repository protocols and Odoo read-only implementation
- deterministic product and tax matching
- ERP-neutral Vendor Bill builder

## Architecture Foundation

This documentation foundation formalizes:

- ICT IPP as the internal architecture name
- Decision Engine inside ICT IPP
- Rule Engine before AI
- AI Advisor as advisory-only
- Company Memory as context, not authority
- Import Workbench as Odoo UI only
- Import Session as the audit unit for imports and recommendations
- Odoo and future ERPs as adapters
- procurement traceability as a durable architectural goal

## Near-Term Implementation Themes

Future implementation should be issue-backed and sliced along these themes:

1. Consolidate deterministic rule evaluation behind a Rule Engine interface without changing current matching behavior.
2. Introduce Decision Engine workflow/strategy selection using existing services as execution steps.
3. Model Import Session state and audit events before building UI workflows.
4. Add Company Memory structures only after retention, privacy, and review rules are accepted.
5. Integrate local Ollama AI only after Rule Engine output is available as input context.
6. Build or customize Odoo Import Workbench screens as UI-only surfaces.
7. Extend procurement traceability links across quotation, RFQ, purchase order, vendor bill, actual cost, and profitability.
8. Add future ERP adapters behind repository and adapter boundaries.

## Out Of Scope Until Separately Approved

- Uyumsoft status-changing operations.
- Odoo invoice posting.
- Odoo unlink, payment registration, reconciliation, or automatic master-data mutation.
- AI-driven automatic approval, matching, strategy selection, or ERP writes.
- Large rewrites of accepted architecture.
- Unbounded sync or import jobs.

## Milestones

Existing delivery milestones remain useful:

- `Foundation`: bootstrap, CI, Docker, WSDL discovery, read-only probes, read-only listing, metadata persistence.
- `Invoice Sync`: incremental sync, run tracking, safe scheduling, idempotent refresh behavior.
- `Attachments`: XML/UBL download, parser, attachment storage, parse diagnostics.
- `Odoo Integration`: read-only mapping preview, controlled Odoo draft invoice creation, Odoo idempotency.
- `Production Ready`: gates, runbooks, monitoring, backup/restore, operational security, go-live checklist.

Proposed IPP architecture milestones for future issues:

- `IPP Decision Foundation`: Decision Engine, workflow selection, strategy selection.
- `IPP Rule Foundation`: deterministic rule catalog, rule execution results, rule audit trail.
- `IPP Import Experience`: Import Session model and Odoo Import Workbench UI-only flow.
- `IPP Advisory AI`: local Ollama integration, Company Memory retrieval, recommendation review controls.
- `IPP Traceability`: procurement trace graph, actual cost links, profitability reporting inputs.

## Related Documents

- [Vision](VISION.md)
- [Architecture](ARCHITECTURE.md)
- [Development Workflow](DEVELOPMENT_WORKFLOW.md)
- [Architecture Decisions](adr/README.md)
