# ICT Intelligent Procurement Platform (IPP) — Project Constitution

## Identity

- External product name: **ICT Integration Hub**
- Internal architecture name: **ICT Intelligent Procurement Platform (IPP)**
- Vision: **AI-assisted procurement automation built on deterministic business rules.**

## Purpose

ICT IPP receives supplier invoices from Uyumsoft, converts UBL documents into an ERP-neutral domain model, evaluates the correct business workflow, and applies approved decisions to Odoo while preserving procurement and profitability traceability.

## Source of Truth

Use sources in this order:

1. `main` branch code
2. This constitution
3. `docs/architecture.md`
4. `docs/vision.md`
5. `docs/roadmap.md`
6. Accepted ADRs under `docs/adr/`
7. Current issue and pull-request context

Accepted ADRs are authoritative. Changes that conflict with an accepted ADR require a new superseding ADR.

## Non-negotiable Architecture Principles

- Clean Architecture
- Lightweight Domain-Driven Design
- Repository Pattern
- Immutable DTOs
- Deterministic matching and decision rules
- Small, production-ready pull requests
- Business logic remains ERP-independent
- Odoo is an adapter and execution surface, not the decision authority
- No fuzzy partner or product matching in automatic workflows
- AI is advisory only and never performs an ERP write by itself
- Rule Engine always executes before AI Advisor
- Production writes require explicit safety gates and remain auditable
- Procurement traceability is preserved whenever possible

## Decision Boundary

ICT IPP owns:

- Uyumsoft integration
- UBL parsing and validation
- Internal invoice domain
- deterministic partner, product, and tax matching
- Rule Engine
- Decision Engine
- workflow selection
- AI Advisor and company-memory retrieval
- import-session orchestration

Odoo owns:

- user-facing Import Workbench
- ERP master and transactional records
- RFQ and Purchase Order records
- Vendor Bills
- expense and asset records
- sales, opportunity, project, and analytical links
- profitability reporting

**Hub decides; Odoo executes.**

## Supported Workflow Decisions

An incoming invoice can be routed to one of these outcomes:

- match an existing Purchase Order
- create a new RFQ and Purchase Order flow
- create a direct Vendor Bill
- process as an operating expense
- process as a fixed asset
- process as a subscription or service
- send to manual review
- ignore or hide with an auditable reason

## AI Policy

- Deterministic rules have priority.
- AI runs only when no sufficient deterministic rule exists.
- AI produces a recommendation, rationale, and confidence indicator.
- A user or an approved deterministic policy makes the final decision.
- Default AI deployment is local through Ollama-compatible models.
- Company data must not leave the controlled environment by default.
- User-approved outcomes may create or refine deterministic rules.

## Procurement Traceability Requirement

The platform must preserve or reconstruct this chain when applicable:

`Opportunity → Sales Quotation → Sales Order → RFQ → Purchase Order → Vendor Invoice → Vendor Bill → Actual Cost → Sales Profitability`

The goal is not merely invoice posting. The goal is reliable linkage between purchasing cost and the related sale, customer, project, proposal scenario, or opportunity.

## Development Workflow

1. Architecture review
2. ADR when a structural decision is introduced or changed
3. GitHub issue
4. Codex implementation on a dedicated branch
5. Draft pull request
6. Code review
7. Merge
8. Production-safe validation

Each pull request must have one focused responsibility, tests, rollback notes where relevant, and no unnecessary refactor.

## Current Baseline

Completed capabilities include:

- Uyumsoft authentication, invoice listing, and invoice download
- production read-only validation
- UBL parser and immutable `InternalInvoice` domain
- deterministic tax mapping
- read-only ERP repositories with Odoo JSON-2 adapter
- deterministic partner and product matching
- pure Vendor Bill builder and deterministic `account.move` payload generation

The next implementation milestone remains a production-safe Odoo Vendor Bill Write Service. Import Session, Rule Engine, Decision Engine, Workbench orchestration, and AI Advisor follow as separate focused milestones.
