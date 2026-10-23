# V1 Workbench — workflow safety, Direct Vendor Bill default, previous-invoice guidance

Status: implemented in the Hub (this PR); the Studio change is **prepared, not applied**.

## 1. Workflow safety (Hub, live after deploy)

The V1 "Kararı Gönder" request (operator request action `decision`) accepts a workflow
decision only for **Direct Vendor Bill** — the only workflow the Hub executes.

| Odoo row | Before | After |
|---|---|---|
| İş Akışı = Direct Vendor Bill | accepted | accepted (unchanged) |
| Operating Expense / Fixed Asset / Subscription / RFQ (with or without allocation) | candidate data error → retried 5×, then `FAILED` | rejected once: *"Bu sürümde yalnız 'Direct Vendor Bill' iş akışı desteklenir…"* |
| Existing Purchase Order (maps to `vendor_bill` + PO allocation) | could be accepted | rejected once (same message) |
| other unparseable decision fields | retried 5×, then `FAILED` | rejected once: *"Karar bilgileri geçersiz veya eksik…"* |
| Dismiss | accepted | accepted (unchanged) |
| Odoo unreachable / row not found / ambiguous | retried | retried (unchanged) |
| request whose decision is already accepted (replay) | `ALREADY_COMPLETED` | `ALREADY_COMPLETED` (gate not applied) |
| row version changed | `STALE` | `STALE` (unchanged) |

Implementation: `WorkbenchCandidateWorkflowError` (a `WorkbenchCandidateDataError`
subclass, so the legacy decision-ingestion path is unchanged) is raised by the candidate
reader for every workflow/allocation problem; `DecisionRequestHandler` turns candidate
data errors into deterministic `ReviewDecisionError`s, which the ingestion workflow
already closes as `REJECTED` without retries. "Existing Purchase Order" is told apart
from Direct Vendor Bill by its mandatory `EXISTING_PURCHASE_ORDER` allocation, which
Direct Vendor Bill forbids.

## 2. Direct Vendor Bill default (Studio, prepared)

Exactly one Odoo object changes: the `code` of server action **1032 "ICT V1 Kararı
Gönder"**. No view, field, selection value, `ir.default` or Workbench row is created or
written.

| File | sha256 |
|---|---|
| before (= production, = `0fca138`): `docs/odoo_studio/v1_workflow_default/actions/decision_before.py` | `e4971ee7fd8fc09e309a222a9f1116692deab74dcdb7ba3627d830a978303ce0` |
| after: `docs/odoo_studio/v1_operator_buttons/actions/decision.py` | `ca4b360d3f976b8df1cdb2baf204cef4d8262aa703390f23848a3d614cb377e9` |

Behaviour of the new code (Karar = Submit Decision):

* İş Akışı **empty** → `x_studio_selected_workflow = "Direct Vendor Bill"` is written in
  the same write that queues the Hub request (so the Hub reads that value).
* İş Akışı = Direct Vendor Bill → unchanged.
* any **other** value → `UserError` (*"Bu sürümde yalnız 'Direct Vendor Bill' iş akışı
  desteklenir…"*); nothing is written and the operator's selection is kept.
* Dismiss → unchanged (no workflow is written).

Historical rows, accepted decisions, the request ledger and audit evidence are not
touched: the action only runs when the operator presses the button on a
`Karar Verilmeli` row.

### Apply (separately approved production step)

1. Read-only pre-check: `ir.actions.server` 1032 exists once, `model_name =
   x_ipp_import_workbench`, `state = code`, `group_ids = [88]`, and the sha256 of its
   live `code` equals the *before* value above. Any other value → stop.
2. Write `code` = contents of the *after* file (JSON-2 `ir.actions.server/write` or
   Settings → Technical → Server Actions → 1032 → paste).
3. Re-read 1032; sha256 of `code` must equal the *after* value, else perform the rollback.
4. Smoke check on one `Karar Verilmeli` row only with explicit approval (pressing the
   button queues a real decision request).

### Rollback

Write the *before* file back to 1032 `code` and verify its sha256. Hub-side safety
(section 1) stays in force either way: without the Studio change an empty İş Akışı
is still refused by the old action ("Eksik bilgi: İş Akışı").

## 3. Previous-invoice guidance (Hub, live after deploy)

At the **Satın Alma Amacı Seçilmeli** and **Muhasebe İşlemi Seçilmeli** steps of a pending
review, the Yapılacaklar text adds a block *"Geçmiş bilgi — öneri değildir"* with the
same supplier's most recent completed invoice:

* supplier name, previous invoice number and date;
* the purchase purpose and the accounting resolution (treatment, Odoo account label,
  expense category or depreciation model) recorded for that review up to its decision;
* always, for every supplier: *"Geçmiş faturadaki muhasebe bilgileri yalnızca referans
  amaçlıdır. Bu faturanın kullanım amacı ve muhasebe hesabı ayrıca doğrulanmalıdır."*;
* if the supplier's completed invoices carry more than one distinct classification
  (purpose, treatment, account, category, asset account, model): a mixed-purpose warning.

No supplier is special-cased in code: there is no VKN list, no supplier-specific note and
no configuration for it. Supplier-specific accounting caveats (for example legacy-ledger
accounts) are for the accountant to confirm per invoice, which the generic warning asks for.

"Completed" = an accepted `select_workflow`/`vendor_bill` decision whose `execute`
execution reached `completed` (the draft bill exists). The supplier is the review's exact
source VKN; the review itself, other companies, dismissed reviews, dry-run/failed
executions and reviews missing either decision are excluded. At most 10 completed
invoices are inspected.

Guarantees:

* nothing is selected, pre-filled or applied — the step, the completed summary and every
  gate are byte-identical with or without the block;
* no `operating_expense_mappings` row is read or written; no new table; the history is
  recomputed from committed Hub rows on each projection sync and never cached;
* a read failure only hides the block (or its label); the projection still publishes.

### Odoo request budget

The block adds **no Workbench (Studio) call and no Odoo write**. Its only Odoo traffic is
the read-only account label (`account.account` `search_read`, plus the existing reader's
one-time `ir.model.fields` probes), and only when the block is actually shown:

* not read at any other step (supplier, product, decision, technical, done) and not for
  non-pending rows — the Hub history query is skipped there as well;
* not read when the same projection already resolved the same account (and model) for the
  current review's own accounting resolution;
* memoised per account (+ depreciation model) for the synchronizer's lifetime — one
  reconcile run or one runtime use case — so a batch of the same supplier reads it once.
  An unavailable label is not memoised.

Measured with counting fakes (`plan`, zero writes; 4 Workbench calls per projection in
every column):

| Scenario | Baseline `0fca138` | PR head `4778db8` | This revision |
|---|---|---|---|
| 1 invoice (runtime or reconcile) | 4 | 8 | 8 |
| 10 invoices, one supplier — reconcile (one synchronizer) | 40 | 53 | 44 |
| 10 invoices, one supplier — runtime (synchronizer per projection) | 40 | 80 | 80 |
| 10 mixed (4×A, 3×B, 2×no history, 1×A at supplier step) — reconcile | 40 | 51 | 45 |
| 10 mixed — runtime | 40 | 72 | 68 |

Remaining cost: a runtime synchronizer is built per use case, so each shown block still
pays the fixed-asset reader's three `ir.model.fields` probes before the label read. They
are part of the reader that also guards CAPITALIZE_FIXED_ASSET validation and are left
unchanged here.

Activation: deploying the image is enough (guidance fields are already mapped). Existing
rows show the block at their next projection sync; a full refresh would need the approved
`reconcile --apply`, which this PR does not run.

Rollback: redeploy the previous image; no migration and no Odoo object is involved.
