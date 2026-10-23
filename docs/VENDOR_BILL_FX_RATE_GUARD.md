# Vendor Bill Exchange Rate (P1): the invoice rate is authoritative

**Business decision.** For a foreign-currency supplier invoice, the exchange rate that the
original UBL invoice states explicitly and validly is the accounting rate. The Hub applies
it to the **one new draft Vendor Bill** through the standard Odoo field
`account.move.invoice_currency_rate`. It never substitutes Odoo's automatic (TCMB) rate.

The Hub never writes `res.currency.rate`, and it never changes, posts or re-creates any
other bill.

**Always on.** There is no feature flag. TRY bills are untouched: no read is made, and no
rate is sent.

## Rate-selection rule

The rule is applied to the **verified original UBL** (sha256 + UUID). The document is
located and read exactly as the source-tax guard does it, freshly on every attempt.

| # | Check | Blocks as |
|---|---|---|
| 1 | Bill is in the company currency (TRY) | `not_applicable` (allowed; no rate is sent) |
| 2 | Original document is verified (hash, UUID) | `unverified_source_document` |
| 3 | `DocumentCurrencyCode` equals the evidence currency | `source_rate_contradictory` |
| 4 | Exactly one invoice-level `PricingExchangeRate`. Payment/Tax rates are never taken as the accounting rate. | none: `source_rate_missing`; several: `source_rate_contradictory` |
| 5 | `SourceCurrencyCode` = bill currency and `TargetCurrencyCode` = company currency, both stated | `source_rate_direction_invalid` (never inverted or guessed) |
| 6 | `CalculationRate` is a finite, positive number | `source_rate_invalid` |
| 7 | It equals the accepted decision's evidence rate | `source_rate_contradictory` |
| 8 | Any other invoice-level rate for the same currency pair states the same value | `source_rate_contradictory` |

When every check passes, the exact `Decimal` CalculationRate is selected
(`source_rate_selected`). Two further guarantees:

- Free-text notes (for example "... TL ... Kr.") are never parsed.
- Odoo's automatic rate for the bill date is read for **display only**. A difference is shown but never applied, and it does not block.

## Odoo behaviour (Odoo 19 / saas~19.3)

- **Direction.** Odoo's `invoice_currency_rate` is "company currency → document currency": 1/49.1616 = `0.0203410792162989` for 1 USD = 49.1616 TRY. Odoo converts every line as `amount_currency / invoice_currency_rate`, rounded at the company currency. The Hub sends the nearest float of `1 / rate`, because the field is a float.
- **Create.** The field is an editable stored compute (`readonly=False`, `precompute=True`). ORM `create` *protects* given values of such fields, so Odoo's automatic rate for `invoice_date` does not replace the given value. Product lines, tax lines and the payable are computed from it (`_get_product_base_line_currency_rate` → `invoice_currency_rate`).
- **Automatic rate (for comparison).** Reading `res.currency` with `context.date = invoice_date` returns exactly the automatic rate. On prod this resolves the **previous day's** TCMB record: 2026-10-06 resolves to 49.1616, while the record named 2026-10-06 holds 49.1802.

## Execution

1. The rate plan is computed after the bill is built, before the writer's first call, on every attempt (DRY_RUN, EXECUTE, retry, resume). A blocking plan fails the step as `vendor_bill_fx_<status>`: nothing is written.
2. The selected rate is set on the bill (`VendorBill.company_currency_rate`) and sent as `invoice_currency_rate` in the one `account.move` create.
3. **Repository backstop:** a non-TRY bill without a valid rate, or a TRY bill with a rate, is refused before any Odoo call.
4. **After the write** (EXECUTE only), the created or recovered draft is read back read-only. The step then verifies:
   - `invoice_currency_rate` equals the selected rate (6 dp);
   - each product line's `|balance| = round(|amount_currency| × rate)` within 0.01 TRY;
   - tax lines match within 0.01 × the number of product lines;
   - the payable exactly balances the other lines and matches the expected TRY total within the summed tolerances;
   - the bill is still `draft` and in the bill currency.

   On any difference the step fails as `vendor_bill_fx_verification_failed` and names the draft. The draft is never changed, deleted or posted.
5. **Idempotency.** It is unchanged. A retry (including after an uncertain write result) goes through the writer's `invoice_origin` lookup first. That lookup finds the same draft, which is then verified again, so no second bill is created. Once the operator has corrected a draft's rate in Odoo, the retry succeeds with `created=false`.

A failed read before the write (timeout) maps to the normal retryable `vendor_bill_transport_failure`,
and a storage outage maps to `source_fx_facts_unavailable`. A failed read of the created bill *after*
the write maps to `vendor_bill_fx_verification_unavailable`: the message names the existing draft and
tells the operator not to post it or re-enter it by hand; a retry re-verifies the same draft.

## Preview and readback

- **Preview** (`fx_rate_plan`) shows:
  - the UBL rate (`source_rate`), Odoo's automatic rate and `rate_difference`;
  - the rate used for the new bill (`selected_rate`, plus the exact float sent);
  - the expected TRY untaxed, VAT and total, each line rounded half up.

  Blocking plans are reported rather than raised. Read outages are shown as `evidence_unavailable`.
- **Readback** (`fx_verification`) runs the same post-write verification. It reports the actual bill rate, the TRY untaxed, tax and payable, and any mismatches and rounding notes. Monetary verification in the bill currency is unchanged.

## Compatibility

- **Persisted evidence is unchanged.** Pending reviews re-import against fingerprints, because review-required imports keep no receipt. The rate's currency codes are therefore read from the stored UBL and are never added to the evidence. No migration is needed.
- **Existing bills (66, 67, 68, 76) are not modified.** They were created at Odoo's automatic rate, which equals their UBL rates, so their readback verifies.
- **Accepted decisions are preserved.** An accepted, unexecuted foreign-currency decision is now executed with its invoice's own rate.

## Accounting date (unchanged; for information)

On move 76, `date` is 2026-10-20 while `invoice_date` is 2026-10-06. This is standard Odoo
behaviour: `_get_accounting_date` books a purchase document created in the invoice month on
`max(invoice_date, today)`, and one created later on the last day of the invoice month. The
Hub never sends `date`. The rate does not depend on `date`.
