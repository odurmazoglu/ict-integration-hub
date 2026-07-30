# ADR-0004: Decision Engine

- Status: Accepted
- Date: 2026-07-30

## Context

Procurement automation needs a single authority for selecting workflows and execution strategies. Placing that authority inside Odoo would couple business decisions to one ERP.

## Decision

Decision Engine lives inside ICT IPP.

It chooses:

- workflow
- strategy
- next review or execution state

It never lives inside Odoo.

## Consequences

Future implementation must keep Decision Engine logic in Hub application/domain boundaries. ERP adapters may execute selected strategies but must not choose them.

## Related Documentation

- [Workflows](../WORKFLOWS.md)
- [Rule Engine](../RULE_ENGINE.md)
