# Invoice Decision Rules

Invoice Decision Rules are immutable, deterministic contracts for future invoice classification and workflow selection. This slice defines the domain vocabulary only. It does not execute rules, classify invoices, read Odoo, write ERP records, modify runtime execution, or add a user interface.

## Boundary

ICT Integration Hub owns decision rules and deterministic orchestration. Odoo owns ERP business processes and document lifecycles after approved Hub decisions are executed.

Rules may decide ERP-neutral outcomes such as workflow, classification, review requirement, and default accounting context. Rules must not create, update, post, pay, reconcile, or delete ERP records.

Out of scope for these contracts:

- rule execution
- invoice classification
- Odoo models, readers, writers, or UI
- runtime planning or execution changes
- migrations
- ERP writes
- AI, fuzzy matching, similarity scoring, or probabilistic ranking

## Contracts

`InvoiceDecisionRule` is the immutable rule definition. It contains:

- `rule_id`
- `rule_code`
- `rule_version`
- `name`
- `enabled`
- `priority`
- `match`
- `action`

`InvoiceDecisionRuleMatch` contains deterministic match conditions only:

- `company_id`
- `vendor_partner_id`
- `vendor_tax_id`
- `currency`
- `provider_document_type`
- `purchase_order_present`
- `description_contains`
- `product_mapping_id`

`InvoiceDecisionRuleAction` contains ERP-neutral requested outcomes only:

- `workflow`
- `classification`
- `default_department_id`
- `default_analytic_account_id`
- `require_review`
- `require_business_context`

`InvoiceDecisionRulePriority` defines deterministic priority as `tier` plus `rank`. Lower values sort earlier after specificity.

`InvoiceClassificationResult` is a future output DTO for carrying matched rules, a selected rule, or conflicts. This PR does not provide an evaluator that produces it from invoices.

## Precedence

Rule ordering is deterministic:

1. More specific match conditions outrank generic rules.
2. Lower priority tier outranks higher tier.
3. Lower priority rank outranks higher rank.
4. `rule_code` and `rule_version` provide stable tie-breakers.

Exact company, vendor, and tax rules therefore outrank generic rules because they contain more deterministic match conditions. Disabled rules are ignored by ordering and conflict detection.

## Conflicts

Rules with the same enabled match fingerprint and same priority but different actions are conflicts. Conflicts must fail closed in future evaluators. The contract helper `find_invoice_decision_rule_conflicts(...)` detects these definitions without evaluating invoice facts.

Behaviorally equivalent rules have the same match fingerprint and action fingerprint. Equivalent rules are stable and do not conflict merely because they have different rule identities.

## Determinism

Contracts reject malformed values at construction time:

- identifiers must be positive integers
- booleans must be exact booleans
- currency is canonical ISO-4217 text
- text fields must be non-empty and bounded
- `description_contains` must be an immutable tuple of unique canonical terms
- priority values must be non-negative integers

The contracts contain no floating-point fields. Future rule engines must not introduce fuzzy matching, AI scoring, embeddings, similarity scoring, or display-text authority into deterministic invoice decision rules.

## Relationship To Existing Rule Engine

The existing `DeterministicRuleEngine` still performs the current direct Vendor Bill and Manual Review behavior. These new contracts do not change that engine, `DecisionEngine`, Workbench submission, execution planning, runtime persistence, Odoo adapters, or ERP writers.

Future work may add a rule evaluator that consumes these contracts. That evaluator must remain ERP-independent, deterministic, side-effect free, and tested separately before it is connected to invoice import orchestration.
