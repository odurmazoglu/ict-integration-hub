# ADR-0002: Rule, Decision, and ERP Boundary

- Status: Accepted
- Date: 2026-07-21

## Context

Workflow selection must remain testable, deterministic, and portable across ERP systems.

## Decision

Rule Engine, Decision Engine, workflow selection, and orchestration live in ICT IPP.

Odoo contains the Import Workbench and executes approved ERP operations through adapters.

**Hub decides; Odoo executes.**

## Consequences

- Odoo customizations must not become the source of cross-ERP business rules.
- Business logic depends on repository and adapter abstractions.
- A future ERP adapter can reuse the same decision logic.
- Odoo may validate execution constraints but may not silently replace a Hub decision.
