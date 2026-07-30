# ADR-0001: Architecture Principles

- Status: Accepted
- Date: 2026-07-30

## Context

ICT Integration Hub is evolving into ICT Intelligent Procurement Platform (IPP). The platform must remain safe for accounting, external provider access, future ERP adapters, and AI-assisted workflows.

## Decision

ICT IPP is governed by these principles:

- Clean Architecture
- Domain Driven Design
- Repository Pattern
- Immutable DTOs
- ERP-independent business logic
- deterministic matching
- small production-ready PRs
- Odoo is an adapter
- Hub owns business decisions
- AI is advisory only

## Consequences

Business rules must not be hidden inside provider adapters, Odoo UI, HTTP handlers, or AI prompts. Future work must preserve deterministic rule execution, explicit review states, and production safety gates.

## Related Documentation

- [Project Constitution](../PROJECT_CONSTITUTION.md)
- [Architecture](../ARCHITECTURE.md)
