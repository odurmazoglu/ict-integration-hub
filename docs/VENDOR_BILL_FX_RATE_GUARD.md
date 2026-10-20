# Vendor Bill FX Rate Guard (P1)

A foreign-currency (USD, EUR, …) Vendor Bill is converted to TRY by Odoo, using the rate
Odoo resolves for the bill's `invoice_date` (`account.move.invoice_currency_rate`). The Hub
**never sends or changes an exchange rate**, and it never writes `res.currency.rate`. The
guard only **compares** Odoo's rate with the rate on the original UBL invoice, so that a
supplier who used a different rate cannot pass silently.

**Always on.** There is no feature flag. TRY bills are untouched: no Odoo read is made and
their behaviour is the same as before.

## Where it runs

| Step | Behaviour |
|---|---|
| EXECUTE / DRY_RUN | Runs after the bill is built and before the writer's first Odoo call, on every attempt (retry and resume included). A blocking result fails the step closed, so nothing is written, and therefore no bill and no duplicate exists. |
| Preview (`GET …/vendor-bill-preview`) | `fx_rate_check` reports the same check without raising. A failed Odoo read is shown as `odoo_rate_unavailable`. |
| Readback (`GET …/vendor-bill-readback`) | `fx_verification` revalidates the created bill's actual `invoice_currency_rate` and its `amount_total_signed`. A mismatch is reported, never corrected. |

## Rates and rounding

- Rates are compared as TRY per 1 document-currency unit, which is the UBL-TR direction (`SourceCurrencyCode` = bill currency, `TargetCurrencyCode` = TRY). Odoo stores the inverse as a binary float, so the Hub inverts it with `Decimal` and normalizes it to 6 decimals.
- **Direction is proven by the value.** A rate is accepted only when it equals Odoo's bill-currency → TRY rate, which a reversed rate (≈0.0203 instead of 49.16) can never do. A reversed rate (one whose product with Odoo's rate is within 0.1% of 1) is reported as `source_rate_direction_invalid` rather than as a plain mismatch.
- A **match** needs both conditions:
  - the rates are equal at 6 decimals;
  - `TaxInclusiveAmount × rate` gives the same TRY amount under both rates. The rounding is half away from zero, at the TRY precision read from Odoo.
- **Odoo rate source.** The Hub reads Odoo's rate with `res.currency` `search_read`, passing `context.date = invoice_date`. This is exactly the value Odoo puts on the bill. On production Odoo saas~19.3 that value is the **previous day's** TCMB record: 2026-10-06 resolves to 49.1616, the rate on move 76, while the record named 2026-10-06 holds 49.1802. That is why the Hub never re-implements Odoo's rate-date rule.
- **Readback tolerance.** Odoo converts each line separately. The bill's TRY total may therefore differ from `total × rate` by at most `0.01 × (invoice lines + 1)`. Such a difference is reported as a note; a larger one is a mismatch.
- **Free-text notes are never parsed.** Text such as "İKİBİNKIRKBİR TL ONDOKUZ Kr." is not a validated source format.

## Statuses (`error_code` = `vendor_bill_fx_<status>`)

| Status | Meaning | Operator action |
|---|---|---|
| `match` / `not_applicable` | The bill may be created. | — |
| `mismatch` | The UBL rate differs from Odoo's rate, or the TRY totals differ. | Ask accounting to confirm the rate. If they approve the invoice rate, create the bill manually in Odoo with that rate. |
| `source_rate_missing` | The invoice states no usable rate. | Ask accounting which rate applies. |
| `source_rate_direction_invalid` | The rate is stated in the reversed direction (TRY → bill currency). The Hub never inverts rates. | Ask accounting for the bill-currency → TRY rate. |
| `odoo_rate_unavailable` | Odoo has no rate for that date (or falls back to 1.0). | Check the Odoo currency rates for the date and retry. |

A failed Odoo read (timeout) is the normal retryable `vendor_bill_transport_failure`.

## Accounting date (unchanged; for information)

Move 76 has `invoice_date` 2026-10-06 and `date` 2026-10-20. This is **standard Odoo
behaviour**, not a Hub decision: the Hub payload sends `invoice_date` only and never sends
`date`.

For purchase documents, Odoo 19 `_get_accounting_date` works as follows:

- When the bill is created in the **same month** as its invoice date, Odoo books it on `max(invoice_date, today)`.
- When it is created in a **later month**, Odoo books it on the last day of the invoice month.
- Lock dates can move the date further.

Bills 66–68 (created on 09-25 and 09-28, with invoice dates in September) follow the same
rule. Odoo always picks the rate from `invoice_date`, never from `date`.

## Evidence compatibility

The guard reads only the `exchange_rate` that is **already persisted** in decision
evidence, the first `CalculationRate` of Pricing, Payment or Tax exchange rate. **The
persisted evidence shape is unchanged.**

This matters because review-required imports keep no import receipt. A re-imported pending
review is therefore compared with its stored evidence fingerprint, and any new field there
would make every already-ingested foreign-currency review conflict. For the same reason,
`SourceCurrencyCode`/`TargetCurrencyCode` are not added to the evidence: the value
comparison above proves the direction instead.

No migration is needed.
