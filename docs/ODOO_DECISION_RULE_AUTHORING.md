# Odoo Decision Rule Authoring

Odoo is the only business UI for Invoice Decision Rule configuration. ICT Integration Hub remains an integration and orchestration platform: it reads rule configuration, maps it into immutable application contracts, and later may evaluate those contracts without storing editable business rules or providing a Hub-owned configuration UI.

This document defines the Odoo-facing authoring contract only. It does not implement an Odoo reader, Odoo API calls, rule evaluation, Workbench changes, runtime changes, migrations, execution, or ERP writes.

## Ownership

Odoo owns:

- business-user rule authoring
- the editable configuration records
- the standard user experience for maintaining those records
- the Studio model and fields documented here

Hub owns:

- immutable application contracts
- deterministic validation and mapping boundaries
- future read-only adapter ports
- future side-effect-free rule evaluation
- orchestration after an accepted deterministic decision

Hub never owns a configuration UI and never stores editable business rules. Future persistence, if any, must be immutable evidence or audit data, not an editable rule source of truth.

## Odoo Studio Model

Documented Studio model:

- User name: `IPP Decision Rule`
- Technical model: `x_ipp_decision_rule`

This PR documents the model only. It does not generate Studio XML, create Odoo records, or call Odoo APIs.

## Suggested User Fields

| User field | Contract meaning |
| --- | --- |
| Name | Human-readable rule name |
| Rule Code | Stable business rule identity |
| Active | Whether the rule is enabled |
| Priority | Non-negative deterministic priority tier |
| Company | Exact `res.company` Many2one ERP ID |
| Vendor | Exact `res.partner` Many2one ERP ID |
| Vendor Tax ID | Exact vendor tax identifier text |
| Currency | Exact `res.currency` Many2one ERP ID |
| Description Contains | Deterministic text terms |
| Workflow | Odoo selection value mapped exactly to `WorkflowType` |
| Classification Code | Canonical business classification code |
| Require Review | Boolean review requirement |
| Require Business Context | Boolean business-context requirement |
| Rule Version | Positive integer version |
| Notes | Human note for Odoo users only |

## Central Field Mapping

Hub centralizes all Studio field names in `OdooDecisionRuleFieldMapping`. Field names must not be scattered through application services or future adapters.

Default technical field contract:

- `x_studio_name`
- `x_studio_rule_code`
- `x_studio_active`
- `x_studio_priority`
- `x_studio_company_id`
- `x_studio_vendor_id`
- `x_studio_vendor_tax_id`
- `x_studio_currency_id`
- `x_studio_description_contains`
- `x_studio_workflow`
- `x_studio_classification_code`
- `x_studio_require_review`
- `x_studio_require_business_context`
- `x_studio_rule_version`
- `x_studio_notes`

## Mapping Contract

`OdooDecisionRuleAuthoringRecord` represents one already-read, normalized, immutable Odoo rule row. It is not an Odoo reader and it must not expose raw Odoo dictionaries.

The mapping output is always the canonical PR #91 domain contract:

```text
OdooDecisionRuleAuthoringRecord
  -> InvoiceDecisionRule
  -> InvoiceDecisionRuleMatch
  -> InvoiceDecisionRuleAction
  -> InvoiceDecisionRulePriority
```

`DecisionRuleRepository` is the application port for future adapters. It returns only immutable `InvoiceDecisionRule` objects:

```text
list_invoice_decision_rules(company_id=...) -> tuple[InvoiceDecisionRule, ...]
```

The port does not persist, write, cache, evaluate, or return raw provider payloads.

## Validation

Configuration fails closed when malformed. Future adapters must reject rather than silently repair invalid Odoo configuration.

Required validation:

- Rule Code is required and canonicalized as stable identity text.
- Rule Version is required and must be a positive integer.
- Rule Code plus Rule Version must be unique.
- Active must be an exact boolean.
- Priority must be an integer greater than or equal to zero.
- Workflow must map exactly to a supported `WorkflowType`.
- Classification Code is trimmed, uppercased, and must match `[A-Z][A-Z0-9_]{0,63}`.
- Company must be an exact ERP ID when supplied.
- Vendor must be an exact ERP ID when supplied.
- Currency must be selected by exact ERP ID when supplied.
- The canonical currency code used in `InvoiceDecisionRuleMatch.currency` must come from exact ERP reference validation, not an Odoo display label.
- Description terms must be immutable deterministic text, not fuzzy patterns.
- Missing required fields, invalid workflow, invalid classification, malformed priority, invalid IDs, and duplicate identities are rejected.

## Boundaries

This contract introduces no:

- Odoo reader
- Odoo API call
- XML-RPC
- JSON-RPC
- Workbench change
- runtime change
- rule evaluation
- migration
- execution
- ERP write
- AI, fuzzy matching, similarity scoring, or inferred configuration
