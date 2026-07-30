# ADR-0008: Import Session

- Status: Accepted
- Date: 2026-07-30

## Context

Imports span provider reads, local persistence, parsing, rule results, recommendations, user review, ERP execution, and traceability. Current tables represent pieces of that lifecycle but not a consolidated audit unit.

## Decision

Import Session is the future durable audit unit for ICT IPP imports.

It should group source data, rule results, workflow/strategy decisions, AI recommendations, user review, ERP execution, and traceability outcomes.

## Consequences

Future implementation should reference existing metadata, document, sync run, and draft invoice records rather than losing current idempotency and audit behavior.

## Related Documentation

- [Import Session](../IMPORT_SESSION.md)
- [Import Workbench](../IMPORT_WORKBENCH.md)
