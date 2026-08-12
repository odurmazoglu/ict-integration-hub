# Invoice Decision Rules

Invoice Decision Rules are immutable, deterministic contracts for invoice classification and workflow selection. The contracts and classifier do not write ERP records, modify runtime execution, or add a user interface.

## Boundary

ICT Integration Hub owns decision rules and deterministic orchestration. Odoo owns ERP business processes and document lifecycles after approved Hub decisions are executed.

Rules may decide ERP-neutral outcomes such as workflow, business classification, review requirement, and default accounting context. Rules must not create, update, post, pay, reconcile, or delete ERP records.

Out of scope for these contracts and the deterministic classifier:

- Odoo writers or UI
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
- `classification_code`
- `default_department_id`
- `default_analytic_account_id`
- `require_review`
- `require_business_context`

Workflow answers: "How should this external document enter ERP?"

Workflow remains the closed canonical `WorkflowType` contract because workflow controls supported ERP command paths and must stay code-controlled.

Classification answers: "What business category does this invoice represent?"

Business classifications are rule-configurable canonical codes and do not require Hub code changes. A supplied `classification_code` is trimmed, uppercased, and must match `[A-Z][A-Z0-9_]{0,63}`. It is not a free-form description and it is not a closed application enum.

Examples:

- `EV_CHARGING` -> `WorkflowType.EXPENSE`
- `OFFICE_UTILITY` -> `WorkflowType.EXPENSE`
- `CLOUD_COST` -> `WorkflowType.VENDOR_BILL`

Classification alone never implies ERP execution capability. Only workflow describes the ERP entry path.

`InvoiceDecisionRulePriority` defines deterministic priority as `tier` plus `rank`. Lower values sort earlier after specificity.

`InvoiceClassificationContext` is the canonical ERP-neutral input evidence for configurable rule matching. It contains only supported match facts: company, vendor partner, vendor tax ID, currency, provider document type, purchase order presence, a canonical description corpus, and product mapping IDs.

`InvoiceDecisionRuleEngine` evaluates canonical `InvoiceDecisionRule` values against `InvoiceClassificationContext` and returns `InvoiceClassificationResult`.

`InvoiceClassificationResult` carries a closed status:

- `MATCHED`
- `NO_MATCH`
- `CONFLICT`
- `REVIEW_REQUIRED`

The result includes immutable rule evidence for matched or conflicting winning rules. Classification does not itself perform ERP execution.

## Precedence

Rule ordering is deterministic:

1. More specific match conditions outrank generic rules.
2. Lower priority tier outranks higher tier.
3. Lower priority rank outranks higher rank.
4. `rule_code` and `rule_version` provide stable tie-breakers.

Exact company, vendor, and tax rules therefore outrank generic rules because they contain more deterministic match conditions. Disabled rules are ignored by ordering and conflict detection.

## Conflicts

Rules with the same enabled match fingerprint and same priority but different actions are conflicts. The contract helper `find_invoice_decision_rule_conflicts(...)` detects those definitions without evaluating invoice facts.

During invoice classification, all enabled rules are evaluated. If no rule matches, the result is `NO_MATCH`. If rules match, the engine considers the highest effective precedence level using the existing specificity and priority semantics from `order_invoice_decision_rules(...)`. Equal-winning rules with the same action fingerprint produce one deterministic `MATCHED` or `REVIEW_REQUIRED` result. Equal-winning rules with incompatible action fingerprints produce `CONFLICT` with safe immutable rule evidence.

Behaviorally equivalent rules have the same match fingerprint and action fingerprint. Equivalent rules are stable and do not conflict merely because they have different rule identities.

## Determinism

Contracts reject malformed values at construction time:

- identifiers must be positive integers
- booleans must be exact booleans
- currency is canonical ISO-4217 text
- classification code is canonical deterministic text, not an enum of business categories
- text fields must be non-empty and bounded
- `description_contains` must be an immutable tuple of unique canonical terms
- priority values must be non-negative integers

The contracts contain no floating-point fields. Future rule engines must not introduce fuzzy matching, AI scoring, embeddings, similarity scoring, or display-text authority into deterministic invoice decision rules.

Description matching is a case-normalized substring check against the canonical description corpus. Every configured `description_contains` term must be present. There is no token similarity, stemming, synonym expansion, locale inference, or hidden delimiter guessing.

## Relationship To Existing Rule Engine

The existing `DeterministicRuleEngine` still performs the current direct Vendor Bill and Manual Review behavior. `InvoiceDecisionRuleEngine` is a separate configurable-rule classifier in this slice. It does not change `DeterministicRuleEngine`, `DecisionEngine`, Workbench submission, execution planning, runtime persistence, Odoo adapters, or ERP writers.

Future work may connect classification evidence into import orchestration or Workbench projection with an explicit migration plan. This slice stops at deterministic classification evidence.

## Odoo Configuration Source

Odoo is the business-user authoring surface for these contracts. `OdooDecisionRuleRepository` is a read-only infrastructure adapter that reads active Studio-authored `IPP Decision Rule` rows for a requested company plus shared rows, validates exact ERP IDs and canonical stored values, and returns only immutable `InvoiceDecisionRule` objects through the application `DecisionRuleRepository` port.

The adapter is configuration ingestion only. It does not evaluate rules, classify invoices, write Odoo, call providers, touch Workbench decisions, or change runtime execution. The application classifier consumes only canonical rules and context; it has no Odoo dependency.
