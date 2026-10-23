# Workbench DISMISS — "Muhasebeleştirme Dışı Bırak"

`dismiss` is one of the two explicit Workbench review decisions (the other is
`select_workflow`). This page states what a dismissal means and does not mean.

## What it is

- **A Hub accounting-processing decision.** It records that the Hub will not process this
  invoice into Odoo accounting. The Hub never creates a Vendor Bill for a dismissed review:
  accepted-decision execution returns `not_executable`, the Workbench Vendor Bill trigger
  refuses it, and no Vendor Bill write authorization can be issued for it.
- **Not a statutory e-Fatura rejection ("ret") or cancellation ("iptal").** The Hub sends
  nothing to Uyumsoft or GİB for a dismissal and never changes the provider-side invoice
  status. Any statutory response, return, correction or cancellation — and the accounting
  treatment of the invoice — remains the accountant's responsibility, outside the Hub.
- **Non-destructive.** The original UBL document, the Uyumsoft source metadata, the source
  invoice evidence and the classification history are all retained. Only the review's
  `status` (→ `dismissed`) and `version` change, and one immutable
  `workbench_review_decisions` row is written (`decided_by`, `submitted_at`,
  `review_version_before/after`, `comment`).
- **Terminal in the current implementation.** A dismissed review accepts no further
  decision, reclassification, source-identity correction, supplier / purpose / accounting
  / product resolution. A re-import of the same invoice (same import idempotency key)
  returns `already_imported` and never reopens it. **Reversal is not supported through the
  app**; reopening a dismissed review would need a separately approved design with its own
  authorization and audit trail.

## A new dismissal requires an explanation

Every new `dismiss` decision must carry a `comment` of at least 10 characters after
trimming surrounding whitespace (`DISMISS_COMMENT_MIN_LENGTH`; at most 1000 characters).
Example: `"Kişisel harcama — şahsi ödeme"`. A one-word note such as `"kişisel"` is refused.

The rule is enforced in `SubmitReviewDecisionUseCase`, the one boundary every public entry
point passes, so no path can create a comment-less dismissal:

| Entry point | Missing, blank or shorter than 10 characters |
|---|---|
| `POST /api/workbench/reviews/{review_id}/decision` | HTTP 422, error code `dismiss_comment_required` |
| `POST /api/workbench/decisions/sync` (Odoo Workbench "Dismiss") | candidate status `invalid_decision`; nothing persisted; the Odoo row is not acknowledged |
| ADR-0013 operator request "Karar" | request outcome `rejected` with the actionable message |
| Odoo Workbench submission orchestrator | the same `DismissCommentRequiredError` |

`select_workflow` keeps its optional comment, unchanged and untrimmed.

Backward compatibility: the rule applies to new submissions only. Persisted decisions are
never rewritten; an exact replay of an already persisted dismissal (same idempotency key and
fingerprint) still returns the existing acknowledgement, including one recorded before this
rule without a comment. The decision enum value stays `dismiss` and the Odoo Studio
selection values stay `Dismiss` / `Dismissed`.

## Operator wording

Odoo guidance for a dismissed review reads: *"Muhasebeleştirme dışı bırakıldı. Hub bu belge
için Vendor Bill oluşturmaz. Bu işlem yasal e-Fatura reddi veya iptali değildir."* Return
and withholding guidance offers the same decision ("nedenini Karar Notu alanına yazıp
'Muhasebeleştirme Dışı Bırak' düğmesine basın") with the same statement, instead of
"incelemeyi reddedin". An accepted operator dismissal request reports: *"Muhasebeleştirme dışı
bırakıldı; Hub bu fatura için alış faturası oluşturmaz. Bu işlem yasal e-Fatura reddi veya
iptali değildir."*

## Dismissal at any open step (V1)

A dismissal needs only the review, its current version, the requester and the reason. When a
Workbench decision candidate is a `Dismiss`, the candidate reader ignores — and does not read —
any workflow, supplier, line, tax or business-context allocation inputs left on the row. A
review can therefore be excluded at the supplier, product, purpose, accounting, technical or
decision step without creating or matching a supplier first. The Hub still accepts a decision
only for a `pending_review` row at its exact version, so an accepted decision, an executed
Vendor Bill or a closed review cannot be dismissed. The V1 Odoo button is described in
`docs/ODOO_WORKBENCH_V1_DISMISS.md`.

A dismissal is not an accounting classification. An invoice that should not be booked through
the Hub (for example a personal purchase invoiced to the company) may still need its own
treatment in the legacy books; that decision stays with the accountant.
