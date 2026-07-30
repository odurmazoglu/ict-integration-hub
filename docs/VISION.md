# Vision

ICT Integration Hub is the product surface for ICT Teknoloji's integration platform. ICT Intelligent Procurement Platform (IPP) is the internal architecture that turns invoice ingestion, procurement workflows, ERP interaction, matching, and advisory AI into a coherent system.

## Vision Statement

ICT IPP enables AI-assisted procurement automation built on deterministic business rules.

The platform should help teams import, validate, match, trace, and execute procurement documents with less manual effort while preserving finance control, ERP independence, and auditability.

## North Star

The Hub should become the place where procurement decisions are made, explained, and traced. ERP systems should execute those decisions and remain reliable systems of record, but should not become the place where integration-specific business logic is hidden.

## What The Platform Optimizes For

- correctness before convenience
- traceability before speed
- deterministic rules before AI suggestions
- explicit user review before accounting impact
- provider safety before automation depth
- small production-ready delivery before broad rewrites

## Current Product Stage

The current repository implements a safe integration foundation:

- read-only Uyumsoft e-Fatura metadata sync
- UBL XML document retrieval and local storage
- local UBL parsing
- Odoo mapping preview
- deterministic read-only Odoo resolution
- draft-only Odoo vendor bill creation after explicit confirmation
- production gates and validation runbooks

The broader IPP concepts documented in this foundation, such as Decision Engine, Rule Engine, Company Memory, Import Workbench, and Import Session, are accepted architecture directions. They must be implemented incrementally in future PRs.

## Operating Model

1. The Hub ingests provider data safely.
2. Domain models normalize business information.
3. Rule Engine evaluates deterministic policies.
4. Decision Engine selects workflow and strategy.
5. AI Advisor provides recommendations only after rules.
6. Users review unresolved or high-risk states through UI surfaces.
7. ERP adapters execute approved decisions.
8. Traceability links are preserved for profitability analysis.

## Success Criteria

The platform is successful when:

- repeated imports are idempotent
- matching is deterministic and explainable
- ambiguous data is reviewable instead of silently selected
- draft ERP records are traceable to source documents
- AI recommendations improve review quality without replacing business rules
- future ERP adapters can be added without rewriting core business workflows
- source-to-profitability traceability survives normal procurement flows

## Related Documents

- [Project Constitution](PROJECT_CONSTITUTION.md)
- [Architecture](ARCHITECTURE.md)
- [Roadmap](ROADMAP.md)
- [Procurement Traceability ADR](adr/ADR-0009-procurement-traceability.md)
