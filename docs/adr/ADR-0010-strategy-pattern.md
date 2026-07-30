# ADR-0010: Strategy Pattern

- Status: Accepted
- Date: 2026-07-30

## Context

IPP will need multiple workflow execution paths, such as read-only ingestion, manual review, blocked import, draft vendor bill creation, and future procurement traceability workflows.

## Decision

Use explicit strategies for workflow execution paths. The Decision Engine chooses the strategy. Strategy implementations execute through application services, domain rules, repositories, and adapters.

Strategies must be deterministic and testable. They must not hide provider or ERP mutations.

## Consequences

Future strategy code should keep side effects explicit, validate preconditions, and return structured outcomes. Strategy selection must remain inside ICT IPP, not Odoo or AI.

## Related Documentation

- [Workflows](../WORKFLOWS.md)
- [Decision Engine ADR](ADR-0004-decision-engine.md)
