# Operating-Expense Vendor Bill — Controlled Qualification Runbook

Qualifies a real identifier-free operating-expense invoice (e.g. Akyaşam
`AKM2026000004218`) through the production import composition, **stopping before**
any `account.move` write. The deterministic operating-expense matcher is wired into
the sanctioned import composition; the `operating_expense_mappings` table is its only
activation gate (no enabled row → `NOT_FOUND` → Manual Review, exactly as before).

## Preconditions

1. This code deployed and Alembic migrated to head (includes `202607170020`).
2. Production Odoo connectivity verified **read-only**.
3. Production Odoo supplier partner exists with the exact VKN, e.g.
   `Akyaşam Yönetim Hizmetleri A.Ş.` / `0430367181`, so deterministic partner
   matching resolves it. This Hub does not create production partners.
4. An **approved** Odoo `account.account` id for the supplier's operating expense is
   known from Odoo accounting master data. It is never inferred, never a tax /
   analytic / product-category / payable / bank / cash account.
5. The Hub operating-expense mapping is onboarded (Hub DB only):
   ```
   python -m scripts.onboard_operating_expense_mapping \
     --company-id 1 \
     --vendor-partner-id <actual Akyaşam res.partner id> \
     --expense-account-id <approved account.account id> \
     --expense-category OFFICE_OPERATING_EXPENSE \
     --confirm
   ```
   Idempotent: identical re-run → `already_configured`; a divergent
   `expense_account_id` / `expense_category` → fails closed (change is a separate
   administrative action).
6. Execution write gates remain **OFF**: `EXECUTION_EXECUTE_ENABLED`,
   `PRODUCTION_OPERATIONS_ENABLED`, `PRODUCTION_APPROVAL_ACK`,
   `STAGING_VENDOR_BILL_EXECUTE_ENABLED` unchanged.

## Qualification

7. Read the real invoice from Uyumsoft production (read-only).
8. Import it through the production Hub composition
   (`build_uyumsoft_canonical_invoice_importer`).
9. Verify the Odoo Import Workbench review row was created.
10. Verify the workflow recommendation is `VENDOR_BILL`
    (rule `RULE-OPERATING-EXPENSE-VENDOR-BILL-001`).
11. Verify Stage-1 immutable evidence (`workbench_review_execution_evidence`) exists
    for the review version.
12. Verify the pinned `operating_expense_match.expense_account_id` in Stage-1 evidence
    equals the approved account id from precondition 4.

**STOP.** Do not accept the decision or execute `account.move` as part of this phase.
The controlled write authorization happens only after the qualification output above
is inspected and approved.

## Fail-closed guarantees

- No enabled mapping for `(company_id, vendor_partner_id)` → no executable evidence;
  the invoice stays in Manual Review.
- Any invoice line carrying `buyer_item_code` / `seller_item_code` / `barcode` with a
  failed product match → Manual Review; an expense mapping never rescues an unknown
  SKU.
- A mapping for another `company_id` is never used.
- Execution consumes only the immutable Stage-2 evidence; changing the mapping table
  afterwards does not change an already-pinned account.
