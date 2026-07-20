# ADR-0006: Odoo Draft-Only Policy

- Status: Accepted
- Date: 2026-07-20

## Context

Integration Hub prepares Odoo vendor bill data from normalized invoice input and reviewed Odoo resolutions. Odoo accounting authority must remain under authorized users. Automatic posting, payment, or reconciliation would materially change accounting state.

The current implementation creates draft vendor bills only after explicit confirmation and reviewed/resolved Odoo IDs.

## Decision

The integration may create draft vendor bills only.

Current supported Odoo move type is `in_invoice`. The integration must never call `action_post`. It must not perform automatic payment registration, reconciliation, existing invoice updates, unlink, or master-data creation.

Draft creation must use already reviewed and resolved Odoo IDs for partner, currency, journal, products, and taxes. Final accounting approval remains with authorized Odoo users.

## Consequences

### Positive

- Finance users retain control over posting and final accounting approval.
- The integration user does not need posting or unlink permissions.
- Draft records can be reviewed before they affect accounting ledgers.
- Master-data governance remains outside invoice ingestion.

### Negative / Trade-offs

- Human review remains required before accounting completion.
- Operational throughput depends on Odoo user review process.
- Incorrect draft data may still require manual correction in Odoo before posting.

## Alternatives Considered

- Automatically post invoices after draft creation.
- Create missing partners, products, or taxes during invoice processing.
- Allow unlink or automatic cleanup for failed drafts.
- Create customer invoices or other move types in the same workflow.

These alternatives were rejected because they expand accounting authority beyond the approved current scope.

## Operational Notes

- Odoo production permissions should not include invoice posting or unlink.
- Draft creation requires explicit `confirm_create_draft=true`.
- Existing Odoo IDs must come from reviewed mapping/resolution output.
- Production readiness gates do not override the draft-only policy.

## Related Components

- `app/services/odoo_draft_invoice.py`
- `app/schemas/odoo_draft_invoice.py`
- `app/models/odoo_draft_invoice.py`
- `app/api/routers/odoo_mapping.py`

## Related Documentation

- [Production Readiness](../PRODUCTION_READINESS.md)
- [Integration Flow](../INTEGRATION_FLOW.md)
- [Architecture](../ARCHITECTURE.md)
