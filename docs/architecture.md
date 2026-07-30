# Architecture

## High-Level Flow

```text
Uyumsoft
  → Connector Layer
  → UBL XML
  → InternalInvoice Domain
  → Deterministic Matching
  → Rule Engine
  → Decision Engine
  → Workflow Strategy
  → Odoo Adapter
  → Odoo Import Workbench and ERP Records
```

## Core Layers

### Connector Layer

Owns provider communication, authentication, invoice listing, download, integrity checks, and production safety. It contains no ERP business rules.

### Domain Layer

Owns immutable invoice DTOs, UBL parsing, validation, and domain exceptions. It does not depend on Odoo, SOAP, HTTP, or persistence.

### Matching and Mapping

Performs deterministic matching only:

- partner by VKN/TCKN
- product by exact identifiers
- tax by exact company, canonical type, and Decimal rate

Name similarity, fuzzy matching, and AI matching are excluded from automatic matching.

### Rule and Decision Layer

The Rule Engine evaluates deterministic company-scoped rules. The Decision Engine converts rule results and context into a workflow recommendation.

### AI Advisor

Runs only when deterministic rules cannot provide a sufficient decision. It uses company memory and a local model to produce an advisory recommendation. It cannot perform writes.

### Workflow Layer

Executes an approved strategy such as existing-PO matching, RFQ/PO creation, direct Vendor Bill, expense, asset, subscription, manual review, or ignore.

### ERP Adapter

Odoo implementations translate approved workflow commands into Odoo records. Odoo is not allowed to own cross-ERP business rules.

## User Interaction

The user works in an Odoo Import Workbench. The workbench displays source invoice data, matching status, workflow recommendation, related sales/procurement context, warnings, and available actions.

## Traceability

When relevant, all generated or matched records must retain links sufficient to traverse from sale to procurement and actual cost.

## Safety

- read-only by default where possible
- dry-run before production write
- explicit production approval gates
- idempotent creates
- draft records only unless a separate approved workflow posts them
- sanitized logs
- immutable result DTOs
