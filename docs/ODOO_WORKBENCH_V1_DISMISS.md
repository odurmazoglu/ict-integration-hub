# V1 Workbench — "Muhasebeleştirme Dışı Bırak" at any open step

Status: Hub change in PR #244; the Studio objects below are **prepared, not applied**.

## Why

The V1 buttons (view 4221) only offer a decision at *Karar Verilmeli*. An invoice the Hub
must not book — e.g. a personal purchase invoiced to the company — usually stops earlier
(*Tedarikçi Doğrulanmalı*, *Ürün Eşleştirmesi Yapılmalı*, *Teknik Destek Gerekli*), and
reaching the decision step would mean creating or matching a supplier and products for an
invoice that should not be processed at all.

## What changes

### Hub (live after deploy)

* A `Dismiss` decision candidate carries only review, version, requester and reason. The
  candidate reader ignores — and does not read — any workflow, supplier, line, tax or
  allocation input left on the Workbench row, so a half-finished step cannot block or
  leak into a dismissal. `Submit Decision` candidates are parsed and validated exactly as
  before.
* A new dismissal needs a reason of at least 10 characters after trimming
  (`DISMISS_COMMENT_MIN_LENGTH`), on every entry point (REST 422, decision sync
  `invalid_decision`, operator request `rejected`).
* An accepted operator dismissal reports *"Muhasebeleştirme dışı bırakıldı; Hub bu fatura
  için alış faturası oluşturmaz. Bu işlem yasal e-Fatura reddi veya iptali değildir."*
* Unchanged: a decision is persisted only for a `pending_review` row at its exact
  version (so an accepted decision, an executed Vendor Bill or a closed review cannot be
  dismissed); the `workbench_review_decide` permission; idempotency keys; the PR #256
  Direct Vendor Bill gate for `Submit Decision`; no migration.

### Studio (prepared — separately approved step)

No existing object is modified. Two objects are created:

| Object | Definition | Artifact sha256 |
|---|---|---|
| `ir.actions.server` "ICT V1 Muhasebeleştirme Dışı Bırak" | model `x_ipp_import_workbench`, `state = code`, `group_ids = [88]`, unbound; `code` = `docs/odoo_studio/v1_dismiss/actions/dismiss.py` | `c59cbd0227296ebcc74a955d706458215c0fcc3ef1275a9fdc5eebb7c974d36d` |
| `ir.ui.view` "ICT V1 Workbench dismissal" | model `x_ipp_import_workbench`, form, mode extension, `inherit_id` 3984, priority 270; `arch` = `docs/odoo_studio/v1_dismiss/view.xml` with `__SA_DISMISS__` → the new action id | `f9690c63a97c8a4395f08c3b7b28e7ab932f4a45f018f88b63c9a52282977691` (before substitution) |

The view:

* adds the header button **Muhasebeleştirme Dışı Bırak** (group 88) with a confirmation
  dialog: irreversible, no purchase bill, *not* a legal e-Fatura rejection or
  cancellation, nothing sent to the supplier or GİB;
* shows the existing **Karar Notu** field (renamed *Karar Notu / Dışı Bırakma Gerekçesi*)
  and one hint line at every open step.

Both are visible only for `Pending Review` rows without a Vendor Bill, at an open step
(not *Tamamlandı* / *Fatura Oluşturulmalı*, not unsynced), and the button is hidden while a
request is queued. Action 1032 and view 4221 are untouched.

The server action refuses (UserError, nothing written) a queued request, a non-pending row,
a row with a Vendor Bill, a closed or unsynced step, a missing review version and a reason
shorter than 10 characters. Otherwise it writes, in one write: `x_studio_decision =
Dismiss`, the trimmed reason, and the standard "Karar" request fields
(`x_studio_ipp_req_*`). It never writes the workflow, supplier, purpose, accounting or
Vendor Bill fields.

### Apply (separately approved production step)

1. Read-only pre-check: model `x_ipp_import_workbench` (1003), group 88, fields
   `x_studio_decision_comment` (text), `x_studio_decision` (`Dismiss` value),
   `x_studio_review_status` (`Pending Review` value), view 3984 and 4221 active; no server
   action or view with the new names exists.
2. Create the server action with the fields above; read back and verify the `code`
   sha256.
3. Create the view with the action id substituted; read back `arch_db`; verify
   `get_views` on the form contains exactly one dismiss button and that the 4221 buttons
   are unchanged.
4. Optional smoke check, only with explicit approval: open a pending row and confirm the
   button and reason field are shown; **do not press it** — pressing queues a real
   dismissal.

### Rollback

* Studio: set the new view `active = False` (the button and field changes disappear); the
  server action is then unreachable and may stay, or be deleted with approval.
* Hub: redeploy the previous image. No migration and no data change are involved; a
  dismissal already accepted stays (terminal by design, see `docs/WORKBENCH_DISMISS.md`).

## Not covered

* Dismissal is not an accounting classification. Whether an excluded invoice (e.g. a
  personal purchase) needs its own treatment in the legacy books, a statutory response or a
  return is the accountant's decision outside the Hub.
* Reversal of a dismissal is not supported.
