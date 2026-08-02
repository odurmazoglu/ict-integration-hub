# Rule Engine

The Rule Engine is the deterministic policy execution layer for ICT IPP. It lives inside the Hub, executes before AI, and is the source of workflow decisions.

The current implementation provides the `RuleEngine` application port consumed by `DecisionEngine` and the first concrete implementation: `DeterministicRuleEngine`.

## Purpose

The Rule Engine answers questions that must be resolved by explicit business policy:

- Is an import allowed to continue?
- Which workflow should be selected?
- Which strategy should be used?
- Is a candidate match exact, missing, ambiguous, invalid, or not required?
- Is an ERP write allowed?
- Is traceability preserved or broken?

AI may comment on these results after the Rule Engine runs. AI must not replace them.

## Current Deterministic Rules

| Rule area | Current implementation | Behavior |
| --- | --- | --- |
| Runtime production gates | `app/core/runtime_checks.py` | Reject unsafe production configuration before startup/readiness succeeds |
| Uyumsoft read-only boundary | `app/connectors/uyumsoft`, `app/services/uyumsoft_invoice_sync.py`, `app/services/document_service.py` | Allow listing and UBL XML retrieval only |
| Metadata idempotency | `app/services/invoice_persistence.py` | Use ETTN or deterministic fallback identity |
| Document idempotency | `app/services/document_service.py` | Use invoice/document type uniqueness and SHA-256 conflict detection |
| Product matching | `app/matching/product.py` | Match by buyer item code, barcode, then seller item code; stop on ambiguity |
| Tax mapping | `app/tax_mapping/engine.py` | Match active tax by exact company, type, and Decimal rate |
| Odoo resolution | `app/services/odoo_resolution.py` | Resolve existing records with exact deterministic rules only |
| Draft creation validation | `app/services/odoo_draft_invoice.py` | Require confirmation, reviewed IDs, ready preview, ETTN, and non-production gate |
| Vendor bill build validation | `app/billing/builder.py` | Require matched partner, product, tax, invoice header, and valid quantities/prices |
| Direct Vendor Bill rule | `app/application/rules/deterministic.py` | Select `WorkflowType.VENDOR_BILL` only when supplier, product, and tax results are deterministic and complete |

## Implemented Rule: RULE-DIRECT-VENDOR-BILL-001

`RULE-DIRECT-VENDOR-BILL-001` is the first concrete deterministic workflow rule.

It selects `WorkflowType.VENDOR_BILL` only when all prerequisites pass:

- supplier partner matching returns exactly one deterministic match
- supplier matching is not missing, ambiguous, or invalid
- product matching returns one deterministic match for every invoice line
- product matching is not missing, ambiguous, invalid, or incomplete
- tax mapping returns one deterministic tax id for every tax present on invoice lines
- tax mapping is not missing, ambiguous, invalid, or incomplete
- no component-level blocking error is present

Successful output includes an immutable `WorkflowDecision`:

```python
WorkflowDecision(
    workflow=WorkflowType.VENDOR_BILL,
    matched_rule="RULE-DIRECT-VENDOR-BILL-001",
    explanation=("Supplier, products and taxes matched deterministically; Vendor Bill workflow selected."),
)
```

Current deterministic dependencies:

- supplier matching: `PartnerMatchingEngine`, using supplier tax number only
- product matching: `ProductMatchingEngine`, preserving buyer item code, barcode, seller item code priority
- tax mapping: `TaxMappingEngine`, preserving exact company, type, and `Decimal` rate matching

The rule engine coordinates these components through injected dependencies. It does not instantiate Odoo adapters, call ERP APIs, persist records, build Vendor Bills, execute strategies, perform duplicate detection, use AI, or perform fuzzy matching.

## Failure Behavior

Rule failures are safe and explicit application exceptions:

- `PartnerRuleEvaluationError`: supplier missing, ambiguous, invalid, or matcher failure
- `ProductRuleEvaluationError`: product missing, ambiguous, invalid, incomplete, or matcher failure
- `TaxRuleEvaluationError`: tax missing, ambiguous, invalid, incomplete, or mapper failure

When any prerequisite fails, `DeterministicRuleEngine` does not return `WorkflowType.VENDOR_BILL` and does not select another workflow. `WorkflowType.MANUAL_REVIEW` is vocabulary only in this implementation slice.

Component warnings are propagated on `RuleEvaluationResult`. Decision-level explanation remains on `WorkflowDecision`.

## Decision Flow

```mermaid
flowchart TB
    Input[ImportInvoiceCommand] --> Rules[DeterministicRuleEngine]
    Rules --> Partner[PartnerMatchingEngine]
    Rules --> Product[ProductMatchingEngine]
    Rules --> Tax[TaxMappingEngine]
    Partner --> Valid{All required results pass?}
    Product --> Valid
    Tax --> Valid
    Valid -->|yes| Decision[Decision Engine selects workflow and strategy]
    Valid -->|no| Failure[Application-safe rule error]
    Decision --> Advisor[AI Advisor receives rule output]
    Advisor --> Recommendation[Recommendation only]
    Decision --> Execution[ERP adapter executes approved decision]
```

## Workflow Selection

```mermaid
flowchart TB
    Session[Import Session] --> Source{Source document type}
    Source -->|UBL e-Fatura| InvoiceRules[Invoice import rules]
    Source -->|future file/import type| FutureRules[Future deterministic rules]
    InvoiceRules --> DirectVendorBill[RULE-DIRECT-VENDOR-BILL-001]
    DirectVendorBill --> MatchState{Supplier, products, taxes exact?}
    MatchState -->|yes| DraftStrategy[Draft vendor bill strategy]
    MatchState -->|missing, ambiguous, invalid, incomplete| SafeFailure[Application-safe rule error]
```

## Required Rule Result Shape

Future consolidated Rule Engine results should be typed and auditable:

- rule id
- rule version
- input reference
- status: passed, failed, warning, skipped
- deterministic reason
- safe evidence fields
- affected traceability link
- required next action

Rule results must not include credentials, raw XML, SOAP envelopes, full Odoo payloads, or sensitive invoice contents.

## Rule Engine vs Decision Engine

The Rule Engine evaluates facts and policies. The Decision Engine chooses the workflow and strategy using Rule Engine output.

Example:

- Rule Engine: product line 3 has multiple exact candidates by default code.
- Decision Engine: choose manual review strategy instead of draft creation strategy.
- AI Advisor: explain why the line needs review and suggest what data the user may inspect.

Current implementation:

- `RuleEngine` is a port under `app/application/ports`.
- `DeterministicRuleEngine` implements that port under `app/application/rules`.
- `DecisionEngine` calls that port and receives a `RuleEvaluationResult`.
- `DecisionEngine` resolves the workflow from that result through `WorkflowStrategyResolver`.
- This repository currently implements only `VendorBillStrategy`.

The Decision Engine does not evaluate rules itself.

## Rule Engine vs AI Advisor

The Rule Engine is authoritative. AI Advisor is explanatory and advisory.

AI can:

- summarize failed rules
- suggest likely missing data
- explain why a rule blocked automation
- recommend a human-review checklist

AI cannot:

- mark a failed rule as passed
- select an ambiguous candidate
- choose a strategy
- approve ERP writes

## Implementation Guidance

When extending rule evaluation:

- add rules incrementally from existing deterministic behavior
- keep public rule contracts typed
- preserve current status values and safety behavior
- keep rules side-effect free where practical
- test exact inputs, ambiguous cases, invalid data, and safe error output
- update ADRs if the extraction changes authority or behavior

Configurable rule storage, rule administration UI, Rule DSLs, Manual Review execution, and AI Advisor integration are future work and are not implemented by the current Rule Engine.

## Related Documents

- [Decision Engine ADR](adr/ADR-0004-decision-engine.md)
- [Rule Engine ADR](adr/ADR-0005-rule-engine.md)
- [Matching](MATCHING.md)
- [Workflows](WORKFLOWS.md)
