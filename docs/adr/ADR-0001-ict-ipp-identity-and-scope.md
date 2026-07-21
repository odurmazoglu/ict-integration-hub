# ADR-0001: ICT IPP Identity and Scope

- Status: Accepted
- Date: 2026-07-21

## Context

The system evolved from a Uyumsoft-to-Odoo invoice integration into a platform that classifies supplier invoices, recommends workflows, preserves procurement traceability, and supports controlled automation.

## Decision

The internal architecture name is **ICT Intelligent Procurement Platform (IPP)**.

The external product name may remain **ICT Integration Hub**.

The product vision is: **AI-assisted procurement automation built on deterministic business rules.**

## Consequences

- Future features must support procurement decisioning, not only invoice transport.
- Incoming invoices are not assumed to become direct Vendor Bills.
- Documentation, roadmap, and architectural boundaries use ICT IPP terminology.
