# P0-PROD-08M — operator-resolution execution evidence gap

## Discovery

The starting checkout was `claude/account-only-execution-evidence-gap`, based on
`179e20e347a3b56e7b8dac11639fbb715ab82fbf`; fetching origin confirmed the same main
head. Claude left three modified tracked files and an untracked lifecycle test.
The workflow eligibility change was retained after inspection, its commentary was
shortened, and its tests were corrected and expanded. Its test accepting an
unresolved decision contradicted this task and was replaced with rejection.

Delivery uses the isolated branch `codex/p0-prod-08m-execution-evidence-gap` from
that exact base. The original checkout, `backups/`, and untracked product master
data policy are preserved. Case-colliding documentation paths already tracked in
the repository produce unrelated diffs on the local filesystem; those paths are
excluded from the commit.

## Proven control flow and root cause

1. `ImportInvoiceUseCase` calls `DecisionEngine`, creates a pending review, and
   captures the immutable `ReviewSourceInvoiceEvidence` for later reclassification.
2. `DeterministicRuleEngine.evaluate` matches supplier, products and taxes. A real
   unmatched product produces `PRODUCT_NOT_FOUND` and `MANUAL_REVIEW`; expense
   mapping never rescues a line carrying a genuine product identifier.
3. `build_review_execution_evidence` previously returned `None` immediately for
   every workflow except `VENDOR_BILL`. Its existing partial-snapshot branch
   (matched supplier/taxes with product identifiers) therefore could not run for
   the normal `PRODUCT_NOT_FOUND` outcome. Previous tests supplied an artificial
   `VENDOR_BILL` result with an unmatched product, masking the production defect.
4. Supplier remediation calls `ReclassifyWorkbenchReviewUseCase` with
   `SUPPLIER_RESOLUTION`. The use case loads the immutable source invoice and uses
   the same decision engine and shared evidence builder for version N+1.
5. `ReviewReclassificationProposal.new_execution_evidence` is the optional new
   version's matching snapshot. `SqlAlchemyReviewRepository.reclassify_review`
   correctly inserts a row only when that field is present; its absence is an
   upstream builder defect, not a repository insertion defect.
6. `SubmitReviewDecisionUseCase.execute` requires exact
   `(review_id, company_id, expected_version)` Stage 1 evidence **before**
   `_apply_selected_product_resolutions` or `_validate_selected_expense_accounts`.
   The persistence reader raises `ExecutionSourceInvoiceNotFoundError` when the
   row is absent, so no decision or accepted execution evidence can be written.
7. The same circular dependency affects both `selected_product_id` and
   `account_only`: the explicit resolution cannot run without the snapshot that
   the workflow gate prevented from being captured.

## Smallest general correction

Allow `MANUAL_REVIEW` through the existing evidence builder's checks alongside
`VENDOR_BILL`. Matching, reasons, workflow selection, source invoice data and
whole-invoice operating-expense inference remain unchanged. Unmatched supplier,
unmatched tax, unsupported workflows and malformed scope still fail closed.
No parallel evidence representation or fallback reader is introduced.

Stage 1 is immutable source **and matching facts**, not an executable operator
decision. `ReviewClassificationEvidence` separately stores rule provenance; it
does not contain the full product/partner/tax matching snapshot. A partial Stage
1 snapshot leaves unmatched products unchanged and never implies `account_only`.

For every fresh Vendor Bill decision, the application now checks that resolution
lines belong to the invoice, requires explicit accounts for account-only lines,
and reuses `validate_vendor_bill_inputs` after applying validated product
selections and validating selected accounts. Every unresolved non-account-only
line still blocks acceptance. This additional guard is necessary because the
old writer validated snapshot linkage, not line executability. Existing matching
accepted-decision replay skips only the new acceptance guards and retains the
existing evidence-integrity and semantic-equality checks.

Source-derived evidence: complete immutable invoice (including discounts),
supplier match, raw product matches, incoming tax mapping and any already
computed legacy expense-match facts. Operator-derived evidence: validated human
selected product substitutions in Stage 2, and explicit account-only/account id
selections in accepted `line_resolutions`. No source invoice snapshot is mutated.
No vendor-wide expense mapping is created or required.

The existing repository transaction updates the pending review with optimistic
versioning, inserts its decision and captures `ExecutionSourceInvoiceEvidence`
linked to that decision id. Capture failure rolls back acceptance. Product and
account validation happens read-only before that transaction; execution performs
neither lookup nor rematching. Idempotency fingerprints and compare-and-update
conditions remain unchanged.

`SqlAlchemyExecutionSourceInvoiceReader` reconstructs the accepted source plus
its durable decision's line resolutions. `VendorBillExecutionStrategy` and
`VendorBillBuilder` consume only that pinned data; neither changes in this PR.
Mixed product/account-only invoices are supported. Account-only lines have an
account id without a product id; product lines have a product id without an
explicit account id. The discount fixture retains gross 805.01, allowance 241.50,
net 563.51, VAT 112.70 and payable 676.21 for either resolution path.

## Historical pilot limitation and safety

This correction captures evidence for **future import/reclassification outcomes**.
It cannot retrospectively supply the real pilot's missing version-2 matching
snapshot. The source-only snapshot and rule-provenance row are insufficient to
recover that exact historical supplier/product/tax state without rematching.
Missing historical evidence therefore continues to reject decisions. A separately
authorized follow-up would need to use the normal reclassification lifecycle and
validate its new version; this task does not reclassify, backfill, reset or resume
that pilot. No deployment or production read is performed.

ONE_OFF_VENDOR ownership, partner payload, retirement lifecycle, archive-last
ordering and crash recovery are untouched. Partner 448 is not queried or changed.
Its current state is not independently reverified. No real decision is submitted,
no Vendor Bill/product/supplierinfo/account mapping is created, no gate changes,
and no Odoo or Uyumsoft writes are made. Database writes occur only in isolated
local test fixtures and the disposable local Docker database.

## Schema and rollback

No migration is required: Stage 1 already represents unmatched product DTOs;
Stage 2 already holds derived product matches; accepted `line_resolutions` JSON
already stores explicit account ids. Migration head remains `202607170027`.
No dependencies change. Reverting the code commit restores previous behavior;
existing evidence and accepted decisions require no database rollback. New
partial snapshots remain immutable and execution continues to validate them.

## Validation

Lifecycle tests drive the actual deterministic engine through import and the
supplier-resolution reclassification checkpoint with in-memory persistence and
fake external readers. They cover account/product acceptance, missing/wrong
company account rejection without persistence, missing explicit account, no
inference, mixed resolutions, pinned execution, discount/tax economics, decision
replay, idempotency conflict, stale version, corrupt/missing evidence, unknown
lines, transaction rollback and pre-existing legacy decision replay.

Existing regressions cover Workbench decisions, selected product/account
resolution, both evidence stages, Vendor Bill builder/strategy, ONE_OFF_VENDOR,
08L discounts and tax mapping. Local Docker validation uses checked-in placeholder
configuration and disabled write gates in its own Compose project. No production
credentials or environment files are read or copied.

Verified results:

- Targeted regression suites: **399 passed**.
- Full `pytest` (including all unit tests): **2357 passed, 6 skipped**.
- Host `ruff check .` and `ruff format --check .`: passed.
- Python 3.12 Docker focused regressions: **155 passed**; Ruff and formatting passed.
- Isolated Compose build/start: passed; API/DB healthy; `/health`: `{"status":"ok"}`.
- Existing `alembic upgrade head`: passed; current head **202607170027**.
- Existing Alembic deprecation warnings remain; no new dependency or migration.

The host environment uses Python 3.14. The initial host run selected Apple Git
from the temporary directory and hit an Xcode-license error in the migration
comparison test. Re-running with Homebrew Git on PATH passed without changing
migration validation. GitHub CI validates the complete suite on Python 3.12.
