# Odoo Decision Rule Authoring

Odoo is the only business UI for Invoice Decision Rule configuration. ICT Integration Hub remains an integration and orchestration platform: it reads rule configuration, maps it into immutable application contracts, and later may evaluate those contracts without storing editable business rules or providing a Hub-owned configuration UI.

This document defines the Odoo-facing authoring and read-only mapping contract. It does not implement rule evaluation, Workbench changes, runtime changes, migrations, execution, or ERP writes.

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
| Provider Document Type | Deterministic source/provider document classification when available |
| Purchase Order Present | Tri-state PO evidence condition |
| Description Contains | Deterministic text terms |
| Product Mapping | Exact canonical product mapping/reference ID |
| Workflow | Odoo selection value mapped exactly to `WorkflowType` |
| Classification Code | Canonical business classification code |
| Require Review | Boolean review requirement |
| Require Business Context | Boolean business-context requirement |
| Rule Version | Positive integer version |
| Notes | Human note for Odoo users only |

## Central Field Mapping

Hub centralizes all Studio field names in `OdooDecisionRuleFieldMapping`. Field names must not be scattered through application services or future adapters.

Default technical field contract:

- `x_name`
- `x_studio_rule_code`
- `active`
- `x_studio_priority`
- `company_id`
- `x_studio_vendor_id`
- `x_studio_vendor_tax_id`
- `x_studio_currency_id`
- `x_studio_provider_document_type`
- `x_studio_purchase_order_presence`
- `x_studio_description_contains`
- `x_studio_product_mapping_id`
- `x_studio_workflow`
- `x_studio_classification_code`
- `x_studio_require_review`
- `x_studio_require_business_context`
- `x_studio_rule_version`
- `x_studio_notes`

The defaults intentionally prefer standard Odoo capabilities when they exist:

- `active` uses Odoo's standard archive/active semantics instead of an `x_studio_active` duplicate.
- `x_name` is the Studio model record display field. If a deployment uses a standard `name` display field instead, configure the mapping rather than duplicating display text.
- `company_id` should use Odoo's standard company-aware field when the Studio model supports it. If the actual Odoo Online setup requires a Studio Many2one field instead, configure the mapping and document the deployment-specific technical name.

All other fields are documented Studio fields by default. The mapping DTO is configurable because actual Odoo Studio technical names can differ between deployments.

## Mapping Contract

`OdooDecisionRuleAuthoringRecord` represents one already-read, normalized, immutable Odoo rule row. It must not expose raw Odoo dictionaries.

The mapping output is always the canonical PR #91 domain contract:

```text
OdooDecisionRuleAuthoringRecord
  -> InvoiceDecisionRule
  -> InvoiceDecisionRuleMatch
  -> InvoiceDecisionRuleAction
  -> InvoiceDecisionRulePriority
```

`DecisionRuleRepository` is the application port for read-only adapters. It returns only immutable `InvoiceDecisionRule` objects:

```text
list_invoice_decision_rules(company_id=...) -> tuple[InvoiceDecisionRule, ...]
```

The port does not persist, write, cache, evaluate, or return raw provider payloads.

`OdooDecisionRuleRepository` is the production read-only Odoo adapter for this port. It reads the configured Studio model through `OdooReadOnlyAdapter.search_read_all`, maps rows into canonical Hub rule contracts, and fails closed on malformed configuration. It does not call Odoo `create`, `write`, `unlink`, or any ERP business operation.

The adapter boundary is deliberately split:

```text
Odoo read-only transport
  -> raw adapter row data
  -> Odoo infrastructure mapper
  -> OdooDecisionRuleAuthoringRecord
  -> canonical InvoiceDecisionRule
```

The repository builds the company/shared Odoo query, delegates row parsing to the mapper, rejects duplicate rule identities, and returns deterministic ordering. The mapper owns raw Odoo row parsing, exact Many2one ID extraction, stored selection parsing, description-term parsing, currency reference resolution, and creation of the immutable authoring record. Neither layer evaluates rules or exposes raw Odoo rows to application services.

The repository query is company-scoped:

```text
active = true
and (
  company_id = requested company
  or company_id is empty
)
```

Company-specific and shared rules can therefore be read together, but rules for another company are never returned.

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
- Currency must be selected by exact `res.currency` Many2one ERP ID when supplied.
- The canonical currency code used in `InvoiceDecisionRuleMatch.currency` must come from exact ERP reference validation, not an Odoo display label.
- Provider Document Type is optional canonical deterministic text and is not inferred from vendor, currency, or invoice description.
- Purchase Order Present must preserve tri-state semantics:
  - unset means do not care
  - true means PO evidence must exist
  - false means PO evidence must not exist
- Odoo should model Purchase Order Present as a Selection using stored values `any`, `required`, and `must_not_exist`, then map deterministically to `None`, `True`, and `False`. Display labels are not authoritative. Do not use a normal Boolean if that collapses unset and false into the same value. If a deployment proves that nullable Odoo Boolean state is preserved safely end to end, document that deployment-specific choice.
- Description terms must be immutable deterministic text, not fuzzy patterns. The Odoo reader treats one non-empty line as one required term and does not split comma-separated prose.
- Product Mapping must be an exact canonical mapping/reference ID when supplied. Odoo display names are not authoritative.
- Missing required fields, invalid workflow, invalid classification, malformed priority, invalid IDs, and duplicate identities are rejected.

Exact ERP reference semantics:

- Company, Vendor, Currency, and Product Mapping are consumed by numeric ID only.
- Many2one display names are ignored.
- Currency IDs are resolved through the read-only `CurrencyReferenceRepository.find_currencies_by_ids(...)`.
- Missing or inactive currencies fail closed.
- Workflow uses the stored Odoo selection value and maps exactly to `WorkflowType`.
- Rules are returned in deterministic domain ordering after validation.

## Boundaries

This contract introduces no:

- XML-RPC
- JSON-RPC
- Workbench change
- runtime change
- rule evaluation
- migration
- execution
- ERP write
- AI, fuzzy matching, similarity scoring, or inferred configuration
