# Architecture Decision Records

Architecture Decision Records (ADRs) document permanent architectural decisions for ICT Integration Hub. They explain why the system is shaped the way it is, the trade-offs accepted, and the operational consequences future work must respect.

ADRs are not sprint history, issue logs, release notes, or approval records. Use issues and pull requests for implementation tracking.

## When To Add An ADR

Add a new ADR when a change establishes or changes a durable architectural boundary, safety policy, integration contract, persistence strategy, production gate, or cross-cutting operational rule.

Examples:

- introducing a new provider boundary
- changing invoice idempotency strategy
- allowing a new class of Odoo write operation
- changing production enablement gates
- adding a new storage backend contract

## When To Supersede An ADR

Do not edit an accepted ADR to reverse its decision. If the architecture changes, add a new ADR that references and supersedes the old one. Minor clarifications, typo fixes, and links to newer documentation may be edited in place when they do not change the decision.

## Status Definitions

- `Accepted`: the decision is current and must guide implementation.
- `Superseded`: a newer ADR replaces this decision.
- `Deprecated`: the decision remains historical context but should not guide new work.
- `Proposed`: the decision is under review and not yet binding.

## Naming And Numbering

ADR files use four-digit sequential numbers and a short kebab-case title:

```text
NNNN-short-title.md
```

Do not reuse numbers. Keep titles stable after acceptance unless a typo prevents understanding.

## ADR Index

| Number | Title | Status | Decision Summary |
| --- | --- | --- | --- |
| [0001](0001-provider-abstraction.md) | Provider Abstraction | Accepted | Keep provider adapters separate from provider-independent invoice processing. |
| [0002](0002-uyumsoft-read-only-policy.md) | Uyumsoft Read-Only Policy | Accepted | Treat Uyumsoft access as read-only by default and forbid provider-side status changes unless separately approved. |
| [0003](0003-provider-independent-normalized-models.md) | Provider-Independent Normalized Models | Accepted | Use normalized invoice models as the contract between ingestion and ERP-specific layers. |
| [0004](0004-odoo-resolution-engine.md) | Odoo Resolution Engine | Accepted | Resolve existing Odoo IDs in a dedicated deterministic read-only layer. |
| [0005](0005-ettn-idempotency.md) | ETTN Idempotency | Accepted | Use ETTN/UUID as the primary idempotency key for invoice processing and draft creation. |
| [0006](0006-odoo-draft-only-policy.md) | Odoo Draft-Only Policy | Accepted | Allow only draft vendor bill creation and keep posting under human Odoo authority. |
| [0007](0007-production-safety-gates.md) | Production Safety Gates | Accepted | Require multiple explicit runtime and operational gates before production access. |
| [0008](0008-local-persistence-after-external-write.md) | Local Persistence After External Write | Accepted | Record external draft creation locally after the Odoo write and reconcile manually if local persistence fails. |
