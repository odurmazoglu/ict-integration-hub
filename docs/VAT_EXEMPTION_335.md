# VAT Exemption Code 335 Mapping (PR T T2-A)

An incoming `ISTISNA` invoice whose original UBL-TR document proves VAT exemption code
**335** (Basılı Kitap ve Süreli Yayınların Teslimleri) on every line is mapped automatically to
the one dedicated Odoo purchase tax accounting approved for that code. **Disabled by default.**
With the default (empty) configuration the Hub behaves exactly as T1: every 0% / missing-Percent
/ exemption VAT line stays `INVALID_INPUT` -> `UNSUPPORTED_INVOICE_CONTENT`, and no Odoo tax is
read for it.

Codes 209 and 351 are **not** approved and stay blocked. There is no manual tax-selection UI.

## Configuration

```
VAT_EXEMPTION_PURCHASE_TAX_IDS={"335": <Odoo account.tax id>}
```

Validated at startup (`app/tax_mapping/exemption.py::validate_vat_exemption_tax_config`); an
invalid value stops the process instead of silently disabling the feature:

| Refused | Why |
| --- | --- |
| any code other than `"335"` (incl. `209`, `351`, `" 335"`, numeric keys) | only 335 is approved |
| tax id not a positive JSON integer (`0`, `-1`, `true`, `"9335"`, `9335.0`) | the id is the identity |
| tax id `6` | the chart's generic 0% purchase tax is never an exemption tax |
| the same tax id for two codes | one dedicated tax per code |

The tax is **never** discovered by name or rate. The configured id is authoritative; the
`335` name token is only an additional guard against configuring the wrong tax.

## Classification (import, reclassification, evidence recovery)

`DeterministicRuleEngine` runs T1 tax mapping unchanged, then `VatExemptionTaxPolicy.map_invoice`.
The T1 results are replaced only when **all** of the following hold
(`assess_invoice_exemption`, all-lines v1 policy):

1. the source-tax assessment is verified (PR T W1: SHA-256 + `cbc:UUID`);
2. `InvoiceTypeCode` is exactly `ISTISNA` in the document **and** in canonical evidence;
3. no withholding content and no non-`0015` tax content (W1 facts);
4. the document's lines / tax subtotals align one-to-one with canonical evidence (same line ids
   in order, same `(line, tax_index)` positions, same Percent / TaxAmount / TaxableAmount /
   exemption reason); line ids are present and unique;
5. every line has exactly one `TaxSubtotal`: `TaxTypeCode` `0015`, `TaxExemptionReasonCode`
   `335`, `TaxAmount` exactly 0, `Percent` absent or 0, `TaxableAmount` present and >= 0, and a
   canonical tax name that normalizes to VAT;
6. invoice-level `TaxSubtotal`s, when present, are the same exempt shape with the same code and
   their `TaxableAmount`s sum to the line `TaxableAmount`s;
7. code 335 is configured, and the configured tax passes the Odoo validation below;
8. T1 produced exactly one `INVALID_INPUT` result per canonical tax and no errors.

Then every line gets:

```
status=MATCHED  tax_type=EXEMPTION  tax_rate=0  tax_id=<configured id>
matched_by="source_exemption_code:335"  candidate_count=1  confidence=1.00
```

If the exemption is proven but the configured tax fails validation, the line stays
`INVALID_INPUT` with the reason *"VAT exemption 335 is proven, but the configured Odoo tax failed
validation."* (structured log `vat_exemption_tax_invalid`). An Odoo read failure is raised, like a
T1 tax-lookup failure: nothing is classified or persisted.

Supplier, product and operating-expense reasons are evaluated as before; a 335 match only
removes the tax blocker.

## Odoo tax validation

`OdooExemptionTaxReader` reads `account.tax` by exact id (archived included) through the
existing allowlisted read-only client. Every field below must be present; any problem blocks:

| Field | Required |
| --- | --- |
| `id` | the configured id (never `6`) |
| `active` | `true` |
| `company_id` | the review's company |
| `country_code` | `TR` |
| `type_tax_use` / `amount_type` / `amount` | `purchase` / `percent` / `0` |
| `children_tax_ids`, `fiscal_position_ids`, `original_tax_ids`, `replacing_tax_ids` | empty |
| `price_include` | `false` |
| `price_include_override` | unset or `tax_excluded` |
| `include_base_amount` / `has_negative_factor` | `false` / `false` |
| `tax_exigibility` | `on_invoice` |
| `name` | contains a delimited `335` token |

**Not validated by the Hub** (outside the Odoo allowlist: `account.tax.repartition.line`,
`account.tax.group`, `account.account.tag`): repartition lines, tax report grids/tags, the tax
account and the tax group's accounts. Accounting confirms them in Odoo before the id is
configured.

## Vendor Bill backstop

Decision acceptance, Vendor Bill preview and every execution attempt (DRY_RUN and EXECUTE, retry
and resume, Vendor Bill / existing-PO / customer-invoice strategies) call
`SourceTaxSafetyCheck.ensure_vendor_bill_tax_allowed`: W1's checks, then
`ensure_pinned_exemptions_allowed` against the **freshly verified** document. If any pinned line
is `EXEMPTION`, has a `source_exemption_code:*` match, or points at a configured exemption tax
id, the whole invoice must again pass the classification policy, every pin must equal the
current configuration exactly (code, tax id, company, rate 0), and a fresh Odoo read must pass
the validation. Otherwise `SourceTaxSafetyBlockedError` (`UNSUPPORTED_INVOICE_CONTENT`) before
any build or write; an Odoo outage is the retryable `SourceTaxFactsUnavailableError`.

Removing the configuration therefore blocks every exemption-pinned execution. A non-exempt line
pinned to the exemption tax id is blocked. Ordinary VAT executions never read the exemption tax.

Residual TOCTOU: the tax is re-read immediately before the build, but Odoo could change it
between that read and the bill write; the Hub cannot lock an Odoo record. Configuration is
process-static (environment), so a change requires recreating `api` and `poller`.

## Enablement (separate approvals)

1. Deploy T2-A with the configuration empty (no behaviour change).
2. Accounting confirms tax group, label, repartition lines, grids/tags and account; the tax is
   created in Odoo by an authorized user.
3. Read-only validation of the new id (all fields above; repartition shown in the Odoo UI).
4. Accounting approves the id; set `VAT_EXEMPTION_PURCHASE_TAX_IDS` in the production env and
   recreate `api` and `poller`.
5. Reclassify the affected review(s) with separate approval.

Rollback:

* **Preferred: empty the setting** and recreate `api` and `poller`. No migration and no
  persisted-schema change; already pinned exemption lines are then blocked at every Vendor Bill
  use.
* **Image rollback below T2-A** is safe only while no review carries an `EXEMPTION` pin. Older
  images read such evidence fine (`TaxType.EXEMPTION` predates T2-A) but trust the pinned tax id
  without the re-proof above. Once a pin is persisted, the T2-A image is the rollback floor
  (same pattern as the W0 floor) unless those reviews are reclassified first.
