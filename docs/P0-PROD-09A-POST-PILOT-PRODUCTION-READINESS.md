# P0-PROD-09A — Post-Pilot Production Readiness & Controlled Rollout Design

Investigation performed after the successful P0-PROD-08W D-Market pilot. Production
writes performed by this task: **zero**. This document is the record of a read-only
investigation plus one new isolated unit test (`tests/unit/test_p0_prod_09a_one_off_vendor_reuse_gap.py`)
that empirically proves a real behavioral gap. No production code was changed.

All findings below are grounded in `origin/main` at `8f9bfbc2516825ba9080df72e0ce39a63f176339`
(confirmed byte-identical to the checked-out local branch for every file cited here — see
"Methodology note").

## Methodology note

The working branch (`claude/account-only-execution-evidence-gap`, local HEAD `179e20e`) is
four merged PRs behind `origin/main` (`8f9bfbc`): #151 (P0-PROD-08M execution-evidence gap),
#153 (fresh review snapshot), #154 (Odoo JSON-2 contract fix), #156 (execution transaction
ownership). Before relying on any file read from the local working tree, every file cited in
this document was diffed against `origin/main` (`git diff HEAD origin/main -- <path>`) and
confirmed to have **zero difference**. Three local files (`review_classification_outcome.py`
and two test files) carry stale, uncommitted local changes that duplicate work already merged
into `origin/main` via PR #151 — these were left untouched (not committed, not discarded) and
are irrelevant to this investigation.

---

## PRODUCTION BASELINE (Phase 0)

Read-only, verified fresh at task start:

| Check | Result |
|---|---|
| Deployed SHA | `8f9bfbc2516825ba9080df72e0ce39a63f176339` |
| Container health | `healthy` |
| Alembic | `202607170027 (head)` |
| `EXECUTION_EXECUTE_ENABLED` | `false` |
| `SUPPLIER_REMEDIATION_WRITE_ENABLED` | `false` |
| `PRODUCT_REMEDIATION_WRITE_ENABLED` | not set (defaults false) |
| `UYUMSOFT_SYNC_EXECUTE_ENABLED` | `false` |
| `ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED` | `false` |
| `CUSTOMER_INVOICE_EXECUTE_ENABLED` | `false` |
| `CUSTOMER_QUOTATION_EXECUTE_ENABLED` | `false` |
| `STAGING_VENDOR_BILL_EXECUTE_ENABLED` | `false` |
| `PRODUCTION_OPERATIONS_ENABLED` | `true` (baseline ack, unrelated to business writes) |
| ONE_OFF_VENDOR retirement (D-Market) | `archived` |
| `workflow_executions` (D-Market review) | 1 row, `state=completed` |
| Odoo `account.move` id 60 | `state=draft`, `amount_total=676.21` |
| Odoo `res.partner` id 448 | `active=False` |

Everything matches the P0-PROD-08W terminal state exactly. Nothing was mutated.

---

## 1. CURRENT FLOW MATRIX (Phase 1)

| # | Stage | Use case | HTTP endpoint | Permission | Write gate | Persistent tables | External ERP call | Idempotency | Operator-usable without SSH? |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Uyumsoft ingestion | `sync_uyumsoft_invoices` handler | `POST /api/v1/sync/uyumsoft/invoices` | n/a (internal) | `UYUMSOFT_SYNC_EXECUTE_ENABLED` | — | Uyumsoft API | invoice UUID/ETTN | Partial — endpoint exists but is a sync/fetch job, not a review action |
| 2 | Immutable source evidence pinned | `ImportInvoiceUseCase` | (triggered by ingestion, no direct endpoint) | n/a | none (Hub-DB only) | `workbench_review_source_invoice_evidence` (immutable) | none | invoice identity | C — internal only |
| 3 | Deterministic matching (partner/product/tax) | `DecisionEngine` + `PartnerMatchingEngine`/product/tax matchers | (part of import) | n/a | none (read-only Odoo) | — | Odoo `res.partner`/`product.product`/`account.tax` (read) | n/a | C — internal only |
| 4 | Review created | `ImportInvoiceUseCase` | (part of import) | n/a | none | `workbench_review_items` | none | idempotency_key = `uyumsoft:{company}:{ettn}` | C — internal only |
| 5 | Review reasons visible | `GetReviewItemUseCase` | `GET /api/workbench/reviews/{review_id}` | `workbench_review_read` | none (read) | — | none | n/a | **A** |
| 6 | Supplier resolution — MATCH_EXISTING | `ResolveWorkbenchSupplierUseCase` | `POST /api/workbench/reviews/{review_id}/supplier-resolution` | `workbench_review_decide` | `SUPPLIER_REMEDIATION_WRITE_ENABLED` (writer path still invoked; no actual create) | `workbench_review_supplier_resolutions`, `..._remediation_effect` | Odoo `res.partner` (read) | resolution row unique per `(review_id, company_id, version)` | **A** (endpoint exists) but gate is closed by default — see Gate Matrix |
| 7 | Supplier resolution — CREATE_PERMANENT_SUPPLIER | same use case | same endpoint, `mode=create_permanent_supplier` | `workbench_review_decide` | `SUPPLIER_REMEDIATION_WRITE_ENABLED` | same + partner write | Odoo `res.partner` create | VAT read-before-write + post-create re-query | **A**, gated |
| 8 | Supplier resolution — ONE_OFF_VENDOR | same use case | same endpoint, `mode=one_off_vendor` | `workbench_review_decide` | `SUPPLIER_REMEDIATION_WRITE_ENABLED` | same + `workbench_review_one_off_vendor_retirements` (new `pending_vendor_bill` row) | Odoo `res.partner` create/reuse | same as #7, plus Hub-ownership check on reuse (see §7 of this doc — **has a real gap**) | **A**, gated |
| 9 | USE_ONE_OFF_SUPPLIER | same use case | same endpoint, `mode=use_one_off_supplier` | `workbench_review_decide` | none (no write occurs) | supplier_resolution row only | none | n/a | **D** — accepted as a mode value but functionally a no-op stub; returns `ONE_OFF_EXECUTION_NOT_SUPPORTED` and never resolves a partner. Deferred/never completed by design. |
| 10 | Product remediation | `CreateNewProductUseCase` | `POST /api/workbench/reviews/{review_id}/product-resolution` | `workbench_review_decide` | `PRODUCT_REMEDIATION_WRITE_ENABLED` | `workbench_review_product_remediation_*` | Odoo `product.template`/`product.supplierinfo` create | read-before-write | **A**, gated |
| 11 | Reclassification | `ReclassifyWorkbenchReviewUseCase` | (triggered internally by #6-10) | n/a | none | `workbench_review_items` (version bump) | Odoo read-only (re-run matchers) | version increments strictly | C — internal only, always fired as a side effect |
| 12 | Stage-1 evidence pinned | `build_review_execution_evidence` | (internal, part of #11) | n/a | none | `workbench_review_execution_evidence` | none | pinned per `(review_id, version)` | C — internal only |
| 13 | Review decision submitted | `SubmitReviewDecisionUseCase` | `POST /api/workbench/reviews/{review_id}/decision` | `workbench_review_decide` | none (Hub-DB persistence only; **does not** touch Odoo) | `workbench_review_decisions` | none | `idempotency_key` request field + expected_version optimistic lock | **A** |
| 14 | `account_only` / `expense_account_id` | part of #13 `LineResolutionRequest` | same endpoint | `workbench_review_decide` | none at this layer (validated by `VendorBillBuilder` at execution time) | part of decision row (`line_resolutions` JSON) | none | n/a | **A** |
| 15 | `selected_product_id` | part of #13 | same endpoint | `workbench_review_decide` | none at this layer | same | none | n/a | **A** |
| 16 | Stage-2 evidence | pinned as part of #13's persisted decision | — | — | — | `workbench_review_decisions` | — | — | **A** (implicit — no separate step) |
| 17 | Execution (Vendor Bill creation) | `RunAcceptedDecisionExecutionUseCase` | `POST /api/workbench/reviews/{review_id}/execute` | `workbench_execute` | `EXECUTION_EXECUTE_ENABLED` (preflight) + `STAGING_VENDOR_BILL_EXECUTE_ENABLED`/`PRODUCTION_OPERATIONS_ENABLED` (writer) | `workflow_executions`, `..._steps`, `..._events` | Odoo `account.move` create (draft only) | dual: `execution_id` (uuid5 of decision identity) + `invoice_origin` (sha256 of plan identity) | **A**, gated |
| 18 | Execution persistence | `ExecutionRuntimeCoordinator` | (part of #17) | n/a | none | same as #17 | none | per-row idempotency key unique constraint | C — internal only |
| 19 | Retry / reconciliation | re-invocation of #17 | same endpoint, same request | `workbench_execute` | same as #17 | same | same | resumes same row, only pending steps re-run | **B** — no dedicated retry verb; re-POSTing `/execute` is the only mechanism, and it is also the only way to *see* `waiting_retry` status (see §6) |
| 20 | ONE_OFF_VENDOR retirement (archive) | `OneOffVendorRetirementTrigger` → `ArchiveOneOffVendorUseCase` | none — fires only as a side effect inside #17 | n/a | `SUPPLIER_REMEDIATION_WRITE_ENABLED` (reused) | `workbench_review_one_off_vendor_retirements` | Odoo `res.partner` write (`active=False`) | archive-last, always-safe read-back via `res.partner.active` | **C** — no standalone endpoint exists at all; explicitly acknowledged in code comments as "currently unwired" outside the post-execution trigger |
| 21 | Final operator-visible result | `POST /execute` response body | same as #17 | `workbench_execute` | — | — | — | — | **A** for the happy path; **C** for anything needing standalone status/retry visibility |

---

## 2. OPERATOR SURFACE MATRIX (Phase 2)

Legend: **A** = supported operator surface, **B** = API exists, no practical standalone
workflow, **C** = engineering/SSH required, **D** = not implemented.

| Capability | Classification | Evidence |
|---|---|---|
| List pending reviews | **A** | `GET /api/workbench/reviews` |
| Inspect review + matching reasons | **A** (summary only) | `GET /api/workbench/reviews/{review_id}` — but note: response carries only summary fields (`invoice_number, supplier_tax_number, supplier_name, invoice_date, currency, total_amount, review_reasons`), **no full line-level source-invoice evidence** (lines, per-line tax, per-line amounts) is exposed via this or any endpoint |
| Resolve supplier — MATCH_EXISTING | **A** (gated) | `POST /supplier-resolution`, `mode=match_existing` |
| Resolve supplier — CREATE_PERMANENT_SUPPLIER | **A** (gated) | same endpoint |
| Resolve supplier — ONE_OFF_VENDOR | **A** (gated), but reuse-of-archived-partner path is a real gap — see §7 | same endpoint |
| Distinguish ONE_OFF_VENDOR from USE_ONE_OFF_SUPPLIER | **D** for USE_ONE_OFF_SUPPLIER — it's a contract stub that never resolves a partner (`ONE_OFF_EXECUTION_NOT_SUPPORTED`), by design, deferred | `supplier_remediation.py` |
| See Hub ownership of a partner | **B** | Only returned as a byproduct field (`one_off_vendor_hub_owned`) of the write-gated `POST /supplier-resolution` call itself — no standalone read |
| See retirement status | **B** | Same as above (`one_off_vendor_retirement_status`) — only visible by re-invoking a write-gated endpoint, not a dedicated GET |
| Select `account_only` | **A** | `POST /decision`, `LineResolutionRequest.account_only` |
| Choose `expense_account_id` | **A** | same, schema-enforced mutual exclusivity with `selected_product_id` |
| Choose `selected_product_id` | **A** | same |
| Submit the decision | **A** | `POST /decision` |
| Preview the resulting Vendor Bill | **D** | No endpoint anywhere computes/returns Vendor Bill content (lines, accounts, taxes, totals) before execution. The only "preview-shaped" mechanism, `mode=dry_run` on `POST /execute`, is a structural no-op confirmation only — it returns `"Dry run completed. No Odoo Vendor Bill was created"` with **no computed payload fields at all**. A separate, unrelated `/api/v1/odoo/mapping-preview` endpoint exists but is not wired to the Workbench review/decision lifecycle (no auth, disabled `draft-invoices` sibling in production) and must not be mistaken for this capability. |
| Execute the accepted decision | **A** (gated) | `POST /execute`, `mode=execute` |
| See execution status | **B** | Only returned synchronously in the `POST /execute` response body itself — no separate `GET` |
| See `waiting_retry` | **B** | Same call as above; there is no way to observe this without re-invoking execute, which also resumes/retries it as a side effect |
| See failed diagnostics | **B** | Same — `status`/`message` fields in the execute response only |
| See created Odoo artifact ID | **B** | `artifacts[]` in the same execute response (`artifact_type`, `artifact_id`) |
| Reconcile uncertain writes | **D** | No dedicated reconciliation endpoint anywhere. `NEEDS_RECONCILIATION`/`RECONCILIATION_REQUIRED` states exist in the domain model but have no operator-facing resolution path other than re-running the whole execution (which is a Vendor-Bill no-op but does retry the archive step as an incidental side effect) |
| Retry a safe failed execution | **B** | Re-POSTing `/execute` with the same `decision_version` is the only mechanism — safe by construction (idempotent), but not a purpose-built "retry" action, and indistinguishable from "check status" |
| Recover ONE_OFF_VENDOR retirement without rerunning Vendor Bill creation | **D** | `ArchiveOneOffVendorUseCase` is composed but never wired to any router; the code explicitly documents this as a "(currently unwired) manual archive path." The only way to nudge a stuck `archive_attempted`/`needs_reconciliation` row forward is to re-run the full Vendor Bill execution (safe no-op on the Vendor Bill side, but not a purpose-built recovery action, and conflates two unrelated operations) |

**Bottom line for Phase 2**: every *write* operation in the happy path (supplier resolution,
product resolution, decision submission, execution) already has a real HTTP endpoint gated by
permission scopes — this is good. What's missing is entirely on the *read/observability* side:
there is no standalone way to see execution status, retirement status, or a Vendor Bill preview
without either (a) triggering a write-capable endpoint as a side effect, or (b) direct DB/Odoo
inspection.

---

## 3. GATE MATRIX (Phase 3)

*(Full detail — file:line citations, composition-vs-request-time analysis, restart blast
radius — is preserved in the investigating agent's report; the operationally load-bearing
conclusions are summarized here.)*

| Gate | Guards | Scope | Requires restart to change? | Narrower control exists? |
|---|---|---|---|---|
| `SUPPLIER_REMEDIATION_WRITE_ENABLED` | `res.partner` create/reuse (CREATE_PERMANENT_SUPPLIER, ONE_OFF_VENDOR) + the archive trigger (reused, same gate) | Global, all companies | Yes | No |
| `PRODUCT_REMEDIATION_WRITE_ENABLED` | `product.template`/`product.supplierinfo` create | Global | Yes | No |
| `UYUMSOFT_SYNC_EXECUTE_ENABLED` | Uyumsoft invoice sync/fetch job | Global | Yes | No |
| `ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED` | best-effort Odoo workbench projection republish (non-critical) | Global | Yes | No |
| `EXECUTION_EXECUTE_ENABLED` | top-level EXECUTE-mode preflight for Vendor Bill / Customer Quotation execution | Global | Yes | No |
| `CUSTOMER_INVOICE_EXECUTE_ENABLED` | Customer Invoice `account.move` writer | Global | Yes | No |
| `CUSTOMER_QUOTATION_EXECUTE_ENABLED` | `sale.order` writer | Global | Yes | No |
| `STAGING_VENDOR_BILL_EXECUTE_ENABLED` | narrow non-production staging bypass, hardcoded host allowlist | Global (also `APP_ENV`/host-gated) | Yes | No (allowlist is a hardcoded frozenset in code) |
| `PRODUCTION_OPERATIONS_ENABLED` | master real-write acknowledgement, combined with `PRODUCTION_APPROVAL_ACK` string + named `approved_by` | Global | Yes | No |

**Key findings, all independently verified against source (not inferred):**

1. **Every gate is a single process-wide boolean on one `Settings` singleton**, sourced via
   `@lru_cache get_settings()`. There is no company-scoping, no per-request override, no
   database-backed flag table, no admin API, no feature-flag SDK anywhere in the codebase.
2. **A container restart is genuinely required to change any gate** — the `@lru_cache` is
   never invalidated in production code (only in test fixtures). There is no lighter-weight
   reload path.
3. **The environment has exactly one process/container/replica** — no `deploy.replicas`, no
   multi-worker uvicorn. A restart to flip a gate **interrupts any in-flight request** and
   **affects every company's concurrent traffic simultaneously**, because there is no
   per-company or per-workflow isolation of any kind.
4. This is precisely the operational shape that made P0-PROD-08W require SSH + `sudo sed -i`
   + `docker compose up -d --no-deps api` for a single invoice: **there is no narrower
   sanctioned mechanism**, and the current architecture provides none.

**Direct answer to the task's framing question #3** (steady-state gate model): continuing to
depend on engineering opening global environment gates per invoice is **not viable as a
steady-state operating model** — every additional invoice would require the same SSH
intervention, the same restart-driven interruption of all concurrent traffic, and the same
engineering approval loop as the pilot. This is the single most consequential
production-readiness gap in the whole system (see Gap Register, BLOCKER-severity item).

---

## 4. OPERATOR UX / API CONTRACT (Phase 4)

This section describes the *minimum* safe controls and information a real accountant/operator
needs, expressed against what already exists vs. what's missing — not a UI design.

**Supplier** — needs: match/missing/ambiguous status (✅ exposed via `review_reasons`),
existing-partner picker for MATCH_EXISTING (⚠️ no dedicated partner-search endpoint —
operator must already know the target `partner_id`), CREATE_PERMANENT_SUPPLIER /
ONE_OFF_VENDOR action (✅ exists), Hub ownership + retirement visibility (⚠️ **B** — only as
write-call byproduct, no read).

**Product** — needs: matched/missing/ambiguous status (✅), product picker (⚠️ no dedicated
Odoo product-search endpoint surfaced to Workbench), POST_AS_EXPENSE/`account_only` toggle
(✅ exists in decision schema).

**Expense** — needs: real Odoo account picker with code/name (❌ **D** — no endpoint returns
a company-scoped `account.account` list for the operator to choose from; the operator must
already know the numeric `expense_account_id`, exactly as engineering had to for D-Market),
company validation (✅ enforced server-side at execution time, but only as a rejection, not a
guided picker).

**Tax** — needs: mapped tax visibility (⚠️ implicit only, via review reasons on ambiguity;
no explicit "here is the matched tax" field on the review detail response), ambiguity/error
state (✅ surfaces as a review reason).

**Invoice** — needs: source totals, discounts, resulting draft preview totals (❌ **D** — see
Preview Gap Analysis, §5; none of this is computable by the operator before executing).

**Decision** — needs: selected workflow, expected version, line resolutions, idempotency
(✅ all present and enforced in `POST /decision`'s request/response contract).

**Execution** — needs: not-started/running/completed/waiting_retry/failed/reconciliation-
required visibility (⚠️ **B** — all values exist in the domain model and are returned in the
`POST /execute` response, but there is no standalone status read, so "checking" and
"acting" are the same call).

**Conclusion**: the *decision* layer (workflow/line-resolution capture) is complete and
well-specified. The *pre-decision guidance* layer (partner search, product search, account
picker with code/name, tax visibility, computed preview) and the *post-decision observability*
layer (standalone execution/retirement status) are the two areas genuinely missing for an
accountant to operate without engineering knowledge of internal Odoo IDs.

---

## 5. PREVIEW CAPABILITY GAP ANALYSIS (Phase 5)

**Finding: no supported preview capability exists.** `mode=dry_run` on `POST /execute` is the
only candidate, and it does not compute or return Vendor Bill content — confirmed by tracing
`OdooVendorBillWriter.write_vendor_bill`: when `command.dry_run` is true it returns
immediately with `status="dry_run", success=True, safe_message="Dry run completed. No Odoo
Vendor Bill was created."` and no line/account/tax/total fields. The response schema itself
(`WorkbenchVendorBillExecutionResponse`/`ExecutionArtifactResponse`) has no fields to carry a
rendered preview even if the backend computed one.

This is a genuine production-readiness gap: P0-PROD-08W's own Phase 3 ("payload preview")
step had to be performed by an engineer, reading `VendorBillBuilder` output via a script,
specifically *because* no supported preview surface exists.

**Design for the smallest safe fix** (not implemented in this task, proposed as P0-PROD-09B —
see Implementation Plan): add a genuine `mode=preview` (or reuse `dry_run` but change its
contract) that:
- Loads the exact same persisted Stage-2 evidence `RunAcceptedDecisionExecutionUseCase`
  already loads for `EXECUTE`.
- Calls the exact same `VendorBillBuilder.build()` used by real execution — the one that
  already enforces the totals invariant and discount preservation (P0-PROD-08L) — to produce
  the mapped Vendor Bill lines.
- Returns that computed content (partner, account/product, taxes, currency, description,
  quantity, unit price, discounts, untaxed, tax, total, idempotency identity) in the response.
- Performs **zero** Odoo writes. Currency/account/tax identifier resolution may remain
  read-only Odoo calls if the existing architecture requires it for display names (e.g.
  resolving `expense_account_id=247` to `"770000 — General Administrative Expenses"`), but
  this must never re-run business matching — it must consume pinned evidence only, exactly as
  the task specifies.
- Must NOT require `EXECUTION_EXECUTE_ENABLED` or any write gate — it's read-only by
  construction, so it should be available to any operator with `workbench_execute` (or even
  `workbench_review_read`) permission, at any time, gate-independent.

This is a genuinely new capability (current code has no code path that runs
`VendorBillBuilder` without also being the real write path), so it is correctly scoped as a
follow-up slice, not an in-task fix.

---

## 6. RETRY / RECONCILIATION MATRIX (Phase 6)

State machine facts (verified via source trace, `app/application/execution/runtime.py`,
`runtime_service.py`, `accepted_decision_use_cases.py`, `one_off_vendor_use_cases.py`):

`workflow_executions.state` ∈ `{NEW, PLANNED, RUNNING, WAITING_RETRY, COMPLETED, FAILED,
CANCELLED}`. `NEW` is audit-log-only (rows are created directly as `PLANNED`). `CANCELLED` is
declared but **unreachable dead code** — no code path ever transitions into it.

| Case | Hub evidence | Odoo evidence | Automatic retry? | Operator retry? | Reconciliation required? | Idempotency identity checked | Never repeat |
|---|---|---|---|---|---|---|---|
| **A.** Failure before any Odoo request (plan-time validation, or safety-gate check inside the writer before any network call) | Depends on where: a `planner.plan()` failure creates **no** `workflow_executions` row at all; a gate-check failure inside the writer creates a row that lands in `WAITING_RETRY`/`FAILED` via the *same* count-based retry policy as every other error (see finding below) | none | No (no scheduler exists) | Yes — safe, re-POST `/execute` | No | `execution_id` (uuid5) + `invoice_origin` (sha256) | Nothing — no write occurred |
| **B.** Deterministic Odoo rejection (e.g. invalid account_id) | Row reaches `WAITING_RETRY` after 1st attempt, `FAILED` after 2nd — **identical treatment to a transient network error**; `error_code` is classified (`vendor_bill_validation_failure` etc.) but **never consulted** by the retry decision (`_should_retry` is a pure `retry_count < max_attempts` counter) | none | Yes, but wastes one attempt uselessly before permanent failure — see Gap Register | Yes, but will fail identically | No, but the wasted-retry-attempt behavior is a real (MEDIUM) gap | same | Nothing — no write occurred |
| **C.** Transport failure before write (timeout during the pre-create `find_existing_vendor_bill` search) | Same `WAITING_RETRY`/`FAILED` progression | none | No | Yes, safe | No | same | Nothing |
| **D.** Uncertain Vendor Bill create (timeout during/after the actual create call) | Row stays in whatever state the runtime left it in (`WAITING_RETRY` most likely) | **Possibly created** — not independently re-verified this session whether `AccountMoveRepository` does its own post-create read-back the way `OdooSupplierPartnerWriter` does; the retry-safety net is the search-before-create on the *next* invocation, not necessarily a same-attempt verification | No | Yes — safe **because** the next `write_vendor_bill` call always does `find_existing_vendor_bill` by `invoice_origin` first | Not modeled as a distinct state in this layer (contrast with the retirement lifecycle's explicit `NEEDS_RECONCILIATION`) | `invoice_origin` (sha256) | Never assume the create failed — always search first, which the code already does |
| **E.** Vendor Bill created + Hub commit succeeds | `workflow_executions.state=COMPLETED`, step `produced_artifacts` records `artifact_id` | `account.move` exists, draft | N/A — terminal success | A repeat call is a verified **no-op at two independent layers**: (1) `create_from_plan`'s idempotency-key lookup returns the existing row unchanged before the coordinator even runs; (2) `ExecutionRuntimeCoordinator.execute()`'s own first line short-circuits on any terminal state and returns the prior result | No | both | **Never** re-attempt a write — and the code already guarantees this |
| **F.** Vendor Bill created + Hub commit fails | This is exactly the scenario PR #156 fixed: `_execute()` now commits unconditionally (success or FAILED/waiting_retry outcome) at its own end, before the retirement trigger ever runs, and `execute()` wraps `_execute()` in `try/except: rollback(); raise` | `account.move` may exist even if Hub state is lost | On next invocation, the row lookup by idempotency key would not find a match if the commit truly never landed (a genuinely lost row) — the *first* recovery is the same search-before-create in the writer, which would find the orphaned Odoo record and correctly mark it "existing" rather than duplicate it | Yes | Only in the true worst case (Hub row never committed) — verified this exact contract via source inspection plus a real production execution in P0-PROD-08W | same | **Never** blind-retry without the search-first step, which is already structural |
| **G.** Hub `waiting_retry` + no Odoo bill | `WAITING_RETRY`, no artifact | none | No | Yes, safe (re-POST resumes the same row, re-runs only the pending step) | No | both | Nothing |
| **H.** Hub failure + Odoo bill exists | Same as F — the search-before-create in the writer is the reconciliation mechanism | `account.move` exists | On retry, `find_existing_vendor_bill` finds it | Yes | No — self-healing by design | `invoice_origin` | Never create a second bill; already guaranteed |
| **I.** Vendor Bill success + archive gate closed | `retirement.status = PENDING_VENDOR_BILL` (correctly, not an error) | Vendor Bill exists, partner still active | No | Operator must re-run execution (which is a Vendor-Bill no-op but retries the retirement trigger as a side effect) OR wait for a future execution attempt — **there is no standalone archive-retry endpoint** | No | retirement row `(review_id, company_id, version)` | Never treat `PENDING_VENDOR_BILL` as failure |
| **J.** Archive transport uncertainty | `retirement.status = ARCHIVE_ATTEMPTED` committed *before* the uncertain remote write (archive-last discipline) | unknown | No | Same as I — only via re-running full execution | Possibly, if read-back also fails (→ K) | `res.partner.active` read-back | Never re-issue the archive blindly; the use case always resumes via read-back |
| **K.** Archive read-back uncertainty (even the read-back fails) | `retirement.status = NEEDS_RECONCILIATION` (terminal) | unknown | No | **None** — this is the one truly stuck state; no HTTP endpoint reaches `ArchiveOneOffVendorUseCase` at all | **Yes — and there is no operator-facing path to it.** This is a concrete gap. | n/a | n/a |
| **L.** Terminal archived state | `ARCHIVED` | `active=False` confirmed | N/A | A repeat call short-circuits (`already_applied=True`) | No | `res.partner.active` | Never re-archive; already a verified no-op |
| **M.** Operator accidentally repeats an already-completed execution request | Both idempotency layers (execution-row lookup, coordinator terminal-state short-circuit) make this a true no-op | unchanged | N/A | Safe by construction | No | both | Nothing happens twice — verified |

**No automatic/scheduled retry mechanism exists anywhere in the codebase** (confirmed by an
exhaustive repo-wide search for scheduler/cron/celery-like code — zero hits). Every retry, for
both Vendor Bill execution and ONE_OFF_VENDOR archive, requires a human (or external caller) to
re-invoke the same HTTP endpoint. For execution this is `POST /execute`; **for the archive
step there is no endpoint at all** — recovery is only ever a side effect of re-running Vendor
Bill execution, which is itself unnecessary once the Vendor Bill already succeeded.

**Direct answer to the task's framing question #5**: `waiting_retry` executions *can* be
safely reconciled without engineering intervention (re-POST `/execute` is safe and
idempotent) — but the *retirement* state's `needs_reconciliation` terminal state **cannot**;
it has zero operator-facing recovery path today. This is the second-most consequential gap
(HIGH severity — see Gap Register).

---

## 7. ONE_OFF_VENDOR REUSE ANALYSIS — future invoice, same VAT (Phase 7)

**This is the single most important empirical finding of this investigation, and it was
directly traced and tested, not assumed, per the task's explicit instruction.**

### What the design intent says

`app/application/workbench/one_off_vendor_use_cases.py` and
`supplier_remediation_use_cases.py` both carry explicit, detailed comments describing the
intended behavior: an exact-VAT match against a partner the Hub previously created via
ONE_OFF_VENDOR — even if that partner is now archived — should be recognized as Hub-owned and
**reused**, not treated as an error:

> "reusing a partner this Hub already archived from an earlier ONE_OFF_VENDOR lifecycle
> (case B/F) is a legitimate, expected state, not an error"
> — `supplier_remediation_use_cases.py:307-314`

There is even ownership-check logic purpose-built for this
(`_create_or_reuse_one_off_vendor_partner`, lines 382-421): if the writer reports
`ALREADY_EXISTS`, it checks `find_one_off_vendor_effect_by_partner_id` to confirm Hub
ownership before treating the match as reuse-eligible (vs. raising
`SupplierResolutionOneOffVendorNotHubOwnedError` for a pre-existing *non*-Hub partner).

### What the code actually does

The real `OdooSupplierPartnerWriter._already_exists_result()`
(`app/erp/write/odoo_supplier_partner_writer.py:240-267`) checks `existing.active`
**unconditionally, before any ownership context is available**:

```python
if not existing.active:
    raise SupplierPartnerInactiveError(
        "The existing Odoo supplier partner for this tax number is archived; resolve it manually."
    )
```

This exception is never caught by `_create_or_reuse_one_off_vendor_partner` or anywhere else
in the call chain — it propagates straight out of `ResolveWorkbenchSupplierUseCase.execute()`.
**The Hub-ownership reuse branch the comments describe is unreachable**: the writer always
fails closed on *any* inactive VAT match, Hub-owned or not, before the ownership check that
would legitimize reuse ever runs.

### Why the existing test suite didn't catch this

`test_f_one_off_vendor_reuses_existing_hub_owned_archived_partner` in
`tests/unit/test_supplier_remediation_orchestration.py` exists precisely to prove this
scenario, and it passes — but it is wired against `_FakeSupplierPartnerWriter`, which
unconditionally returns `ALREADY_EXISTS` for any matching VAT **regardless of the `active`
flag**. It never models the real writer's inactive-partner check at all, so it gives false
confidence: it proves the *orchestration-level ownership-check logic* works in isolation, but
never exercises the real code path that would actually run in production.

### Empirical proof

`tests/unit/test_p0_prod_09a_one_off_vendor_reuse_gap.py` (new, added by this investigation)
reconstructs the exact D-Market post-pilot scenario — a prior `SupplierRemediationEffect`
proving Hub ownership of partner 448, and a real `OdooSupplierPartnerWriter` wired to a fake
Odoo client that returns partner 448 as `active: False` — and resolves a **new** review, same
company, same VAT, `mode=ONE_OFF_VENDOR`. Result, run and passing:

```
SupplierPartnerInactiveError: The existing Odoo supplier partner for this tax number is
archived; resolve it manually.
```

No duplicate partner is created (the fake client asserts `create_res_partner` is never
called) — so there is no data-corruption risk — but no reuse happens either, and no new
`SupplierRemediationEffect`/retirement row is committed for the new review. **The operator is
left with no supported next step through this mode.**

### Direct answer to the task's framing question #6

For the next invoice from VAT `2650179910` (D-Market), attempting `ONE_OFF_VENDOR` resolution:
- Will **not** find and silently reuse partner 448 as a transparent operator experience.
- Will **not** create a duplicate partner (safe).
- **Will fail** with `SupplierPartnerInactiveError`, an unhandled exception surfaced to the
  operator as an error, with no guided recovery path in the current UX/API contract.
- `MATCH_EXISTING` against partner 448 would *also* fail (`_validate_match_existing` in
  `supplier_resolution_use_cases.py` requires `partner.active`).
- There is **no reactivation code path anywhere in the codebase** — grepped exhaustively for
  any write setting `res.partner.active = True`; the only partner write that exists sets it to
  `False`. Reactivation is not a supported operation today, by any mode.

**This means: once a VAT has gone through the ONE_OFF_VENDOR archive-last lifecycle once, the
Hub currently has no supported way to process a second invoice from that same VAT at all**,
short of CREATE_PERMANENT_SUPPLIER (which is a different, deliberate business decision — "make
this vendor permanent" — not an automatic consequence of a second invoice arriving) or
engineering intervention. This is scored BLOCKER-severity in the Gap Register: it directly
affects the ability to process a plausible, likely-common real-world case (a one-off vendor
who turns out not to be quite one-off).

---

## 8. SECOND PILOT READINESS CHECKLIST (Phase 8)

No second invoice was selected or executed. Objective eligibility criteria:

**GO prerequisites** (all must hold):
- [ ] Immutable source evidence is clean (no parse warnings, single ETTN, single supplier)
- [ ] Supplier VAT has **no prior ONE_OFF_VENDOR history** in `workbench_review_supplier_remediation_effect` for this company (given §7's finding — a repeat VAT is currently a guaranteed failure, not a supported path)
- [ ] Supplier resolution mode is deterministically knowable in advance (MATCH_EXISTING against an already-active partner, or CREATE_PERMANENT_SUPPLIER/ONE_OFF_VENDOR against a genuinely first-time VAT)
- [ ] Tax fully and deterministically matched (no `MULTIPLE_MATCHES`/ambiguous tax state)
- [ ] Discount/allowance structure is amount-based (not rate-only) — the `VendorBillBuilder` fail-closed path (P0-PROD-08L) rejects rate-only discounts and discount>gross by design; confirm the target invoice doesn't trip it
- [ ] No unsupported line charges (freight, packaging, or other non-product/non-expense line types not covered by the existing `account_only`/`selected_product_id` contract)
- [ ] Account/product resolution is knowable in advance (either a specific `expense_account_id` an operator can name, or a specific existing Odoo `product.product`)
- [ ] Zero existing uncertain execution (`workflow_executions.state` not in `WAITING_RETRY`/`FAILED`) for the target review
- [ ] Zero idempotency collision — independently re-derive the expected `invoice_origin` key and confirm no matching `account.move` already exists
- [ ] Operator/engineering preview of computed economics matches source invoice totals exactly (manually, until P0-PROD-09B ships a real preview — see §5)
- [ ] Fresh, verified production backup taken immediately before the pilot
- [ ] All reconciliation queries (execution state, retirement state, Odoo read-back) rehearsed and ready, exactly as used in P0-PROD-08W's Phase 7

**STOP conditions** (any one blocks the pilot):
- Target VAT has any prior `SupplierRemediationEffect` row (ONE_OFF_VENDOR or otherwise) —
  given the reuse gap in §7, this is now an explicit STOP, not just a caution
- Tax match is ambiguous or multiple
- Discount structure is rate-only or exceeds gross
- Any gate other than the two strictly required for the pilot's specific step is open
- Production is not at a known, freshly-verified SHA/health/migration state
- A prior uncertain execution or retirement row exists anywhere in the system (even for an
  unrelated review) that hasn't been reconciled

**Post-execution validation**: identical to P0-PROD-08W's Phase 7 — Odoo `account.move`
read-back (state=draft, totals, partner, `invoice_origin`), line-level read-back
(account_id/product_id/tax/description/quantity via nested domain filters), Hub-side
`workflow_executions`/`workflow_execution_steps`/`workflow_execution_events` trace, retirement
status (if ONE_OFF_VENDOR), product/mapping audit (confirm no incidental product/supplierinfo/
expense-mapping creation), gate re-closure confirmation.

**Direct answer to the task's framing question #7**: the smallest set of changes before a
second pilot is *not* strictly required if the second pilot deliberately avoids every STOP
condition above (in particular: a genuinely first-time VAT, to sidestep the reuse gap). But
doing so would just be repeating the same SSH-driven, engineering-mediated operating model as
the first pilot. If the goal is to make a second pilot meaningfully closer to a real steady-
state operating model (not just a repeat of the same one-off engineering exercise), the
minimum useful slice is P0-PROD-09B (preview) — it's the one gap that a real accountant would
hit on literally every invoice, not just edge cases.

---

## 9. PRODUCTION READINESS GAP REGISTER (Phase 9)

| ID | Area | Current behavior | Desired steady-state behavior | Risk if unchanged | Runtime change? | Migration? | Operator surface? | Follow-up |
|---|---|---|---|---|---|---|---|---|
| G1 | ONE_OFF_VENDOR reuse | A second invoice from a VAT that already went through ONE_OFF_VENDOR once fails closed with an unhandled `SupplierPartnerInactiveError`; no reuse, no reactivation path exists | The documented intent (reuse a Hub-owned archived partner) actually works | **BLOCKER** — a plausible, likely-common real case (repeat one-off vendor) has no supported resolution path at all today | Yes | No | Yes (surface the failure meaningfully, or auto-succeed) | P0-PROD-09C |
| G2 | Gate operating model | Every business write gate is a single global env-var boolean requiring a full container restart (SSH + sudo + docker compose) to flip, interrupting all in-flight requests for every company | A narrower, per-operation or per-request sanctioned-write mechanism that doesn't require engineering/SSH per invoice | **BLOCKER** for steady-state — this is exactly what made P0-PROD-08W an engineering exercise rather than an operator action, and it will repeat identically for every future invoice under the current architecture | Yes (significant — new authorization layer) | Possibly (if DB-backed) | Yes | P0-PROD-09D |
| G3 | Vendor Bill preview | No supported capability computes/returns Vendor Bill content before execution; `mode=dry_run` is a structural no-op only | A read-only preview reusing the exact `VendorBillBuilder`/Stage-2 evidence path, no Odoo writes | HIGH — operators cannot verify economics before an irreversible-in-spirit (though draft-only) write; every invoice currently needs an engineering-run preview script exactly like P0-PROD-08W's | Yes | No | Yes | P0-PROD-09B |
| G4 | Retirement reconciliation | `NEEDS_RECONCILIATION` is a real terminal state with zero operator-facing recovery path; only reachable indirectly by re-running full Vendor Bill execution | A dedicated, narrowly-scoped archive-retry/reconcile endpoint | HIGH — an archive left stuck here has no supported recovery at all short of engineering DB/Odoo inspection | Yes | No | Yes | P0-PROD-09E |
| G5 | Execution status visibility | No standalone read for `runtime_state`/`waiting_retry`/artifacts — only returned as a byproduct of the same call that resumes/retries | A dedicated read-only `GET` for execution status per review/decision | MEDIUM — "checking" and "acting" being the same call risks an operator unintentionally triggering a retry while only trying to look | Yes | No | Yes | P0-PROD-09E |
| G6 | Retry classification | Deterministic Odoo rejections (will never succeed) get the exact same one-retry treatment as transient transport failures; `error_code` is computed but never consulted by `_should_retry` | Deterministic/certain-no-write failures should fail immediately without wasting a retry attempt (mirroring the retirement lifecycle's own `_CERTAIN_NO_WRITE_EXCEPTIONS` split, which the execution runtime has no equivalent of) | MEDIUM — wastes one retry cycle, minor operational noise, not a correctness risk | Yes | No | No | P0-PROD-09F |
| G7 | Account/product operator guidance | No endpoint returns a company-scoped Odoo account or product picker; operators must already know numeric IDs | A read-only account/product search surfaced to the Workbench decision UI | MEDIUM — blocks a non-engineer operator from completing `account_only`/`selected_product_id` decisions independently | Yes (new read endpoints) | No | Yes | P0-PROD-09G |
| G8 | Full invoice evidence visibility | `GET /reviews/{review_id}` exposes only summary fields, no line-level detail | Full immutable source evidence (lines, taxes, amounts) exposed to operators | MEDIUM — operators can't independently verify what the deterministic matcher saw | Yes | No | Yes | P0-PROD-09G |
| G9 | `USE_ONE_OFF_SUPPLIER` stub | Exists as a selectable mode but never resolves a partner (deferred by design since P0-3D2D) | Either remove the mode from the operator-facing contract until implemented, or implement it | LOW — currently harmless (records intent only, never partially writes), but a confusing dead-end for an operator who picks it | Maybe (schema-only if removed) | No | Yes | Not scheduled — low priority |
| G10 | Test-suite fidelity gap | `test_f_one_off_vendor_reuses_existing_hub_owned_archived_partner` uses a fake writer that doesn't model the real writer's inactive-partner check, masking G1 | Real-writer-backed test coverage for every ONE_OFF_VENDOR reuse/ambiguity scenario (this task added one; the existing fake-writer test should be paired with, not replaced by, a real-writer equivalent) | MEDIUM — this exact test-suite-vs-reality gap has now recurred twice in this project (see P0-PROD-08M's analogous finding) | No (test-only) | No | No | P0-PROD-09C (bundle with the fix) |

---

## 10. PROPOSED 09B+ IMPLEMENTATION SLICES (Phase 10)

None of these were implemented in this task.

### P0-PROD-09B — Read-only Vendor Bill preview
- **Objective**: let an operator see computed Vendor Bill content before executing, with zero Odoo writes.
- **Scope**: new use case reusing persisted Stage-2 evidence + the existing `VendorBillBuilder`; new response fields on `POST /execute` (or a new `mode=preview`) carrying partner/account/product/taxes/currency/description/quantity/unit price/discounts/untaxed/tax/total/idempotency identity.
- **Explicit non-goals**: no new business matching logic; no Odoo writes of any kind (currency/account/tax identifier reads for display names are the only allowed Odoo calls); no change to the real `mode=execute` path.
- **Files/modules**: `app/application/execution/accepted_decision_use_cases.py`, `app/billing/builder.py` (read path only), `app/schemas/workbench.py`, `app/api/routers/workbench.py`.
- **Migration**: none.
- **Tests**: preview matches real execution's computed values exactly for D-Market's actual pinned evidence (regression); preview never calls any writer; preview available regardless of `EXECUTION_EXECUTE_ENABLED` state.
- **Production safety boundary**: read-only by construction; safe to ship without any gate.
- **Dependency**: none — independent of every other slice.

### P0-PROD-09C — ONE_OFF_VENDOR reuse fix
- **Objective**: make the documented reuse-of-archived-Hub-owned-partner behavior actually reachable.
- **Scope**: `OdooSupplierPartnerWriter` needs to distinguish "inactive AND Hub-owned" from "inactive AND not Hub-owned" — likely by returning `ALREADY_EXISTS` (with an `active=False` flag on the result) instead of raising unconditionally, and letting `_create_or_reuse_one_off_vendor_partner`'s existing ownership check decide the outcome (reuse vs. `SupplierResolutionOneOffVendorNotHubOwnedError`). Requires deciding what "reuse" means operationally — does the review proceed against an *inactive* partner (requiring a reactivation write), or does resolution reuse the identity but require an explicit reactivation step first? This is a real design decision, not a pure bugfix — flag for Onur's input before implementing.
- **Explicit non-goals**: no change to MATCH_EXISTING's requirement that a manually-selected partner be active (that's a different, correctly-strict path); no accounting-policy decision about whether reuse is desirable (system-consequence documentation only, per this task's Phase 7 instruction).
- **Files/modules**: `app/erp/write/odoo_supplier_partner_writer.py`, `app/application/workbench/supplier_remediation_use_cases.py`, possibly a new reactivation writer method.
- **Migration**: none, unless a reactivation write path needs new audit/tracking.
- **Tests**: the real-writer-backed test in `test_p0_prod_09a_one_off_vendor_reuse_gap.py` should flip from expecting `SupplierPartnerInactiveError` to expecting successful reuse, and the existing fake-writer test should be updated or paired with a real-writer equivalent (G10).
- **Production safety boundary**: touches a production write path — needs the same isolated/tested/draft-PR discipline as every prior P0-PROD write-path fix in this project.
- **Dependency**: none.

### P0-PROD-09D — Narrower write-authorization mechanism
- **Objective**: replace "SSH + edit .env + restart container" with a mechanism that can authorize one specific write operation (e.g. one review's execution) without a global, all-company, all-traffic-interrupting restart.
- **Scope**: needs real design work — options include a DB-backed, per-request or per-review authorization token; a short-lived signed approval; or a narrower per-operation gate keyed by review_id. This is the largest slice in this register and should not be scoped further without a dedicated design task.
- **Explicit non-goals**: not a full RBAC overhaul; not a change to who can call which endpoint (that's already permission-scoped) — this is specifically about *replacing the global env-var write gates*.
- **Files/modules**: `app/core/config.py`, every `*WritePolicy`, `app/api/dependencies.py`, likely a new `app/models/` table.
- **Migration**: likely yes.
- **Tests**: extensive — this changes the core safety mechanism of the whole system.
- **Production safety boundary**: this is the highest-risk slice in the register; should be its own multi-phase task with its own investigation, not bundled with anything else.
- **Dependency**: none technically, but should follow 09B/09C so the operator surface is otherwise complete first.

### P0-PROD-09E — Execution/retirement observability + reconciliation endpoints
- **Objective**: standalone `GET` for execution status; standalone reconcile/retry-archive endpoint for `NEEDS_RECONCILIATION`.
- **Scope**: new read-only query use case for `workflow_executions`; new narrow endpoint wiring the already-composed-but-unwired `ArchiveOneOffVendorUseCase`.
- **Explicit non-goals**: no change to the retry policy itself (see 09F); no change to when the archive trigger auto-fires.
- **Files/modules**: `app/api/routers/workbench.py`, `app/composition/supplier_remediation.py` (already has the use case composed), new query use case for execution state.
- **Migration**: none.
- **Tests**: status read returns correct values for every state in the Phase 6 matrix; reconcile endpoint is idempotent and gated by the same `SUPPLIER_REMEDIATION_WRITE_ENABLED`.
- **Production safety boundary**: the status read is pure read-only (safe); the reconcile endpoint touches a real write path (needs the standard discipline).
- **Dependency**: none.

### P0-PROD-09F — Retry classification by error certainty
- **Objective**: don't waste a retry attempt on a deterministic rejection.
- **Scope**: `_should_retry` should consult the already-computed `error_code`, mirroring the retirement lifecycle's `_CERTAIN_NO_WRITE_EXCEPTIONS` split.
- **Explicit non-goals**: no change to `max_attempts` policy value itself; no change to what counts as `WAITING_RETRY` vs `FAILED` beyond this classification.
- **Files/modules**: `app/application/execution/runtime_service.py`.
- **Migration**: none.
- **Tests**: a validation-failure-classified error goes straight to `FAILED` without an intermediate `WAITING_RETRY`; a transport-failure-classified error still gets its one retry.
- **Production safety boundary**: low risk, purely additive classification logic.
- **Dependency**: none.

### P0-PROD-09G — Account/product picker + full invoice evidence endpoints
- **Objective**: let an operator complete `account_only`/`selected_product_id` decisions and verify matching reasons without knowing internal Odoo IDs in advance.
- **Scope**: two new read-only endpoints — company-scoped account/product search, and full line-level invoice evidence on the review detail response.
- **Explicit non-goals**: no change to matching logic itself.
- **Files/modules**: `app/api/routers/workbench.py`, `app/schemas/workbench.py`, new read-only Odoo queries.
- **Migration**: none.
- **Tests**: search results scoped correctly by company; evidence response matches pinned Stage-1 evidence exactly.
- **Production safety boundary**: pure read-only, low risk.
- **Dependency**: none, but naturally pairs well with 09B (preview) for a complete operator experience.

---

## TESTS

New test added and passing: `tests/unit/test_p0_prod_09a_one_off_vendor_reuse_gap.py` (1 test,
real `OdooSupplierPartnerWriter`, zero production/Odoo calls). Full relevant regression suite
re-run alongside it:

```
tests/unit/test_p0_prod_09a_one_off_vendor_reuse_gap.py .
tests/unit/test_supplier_remediation_orchestration.py ......................
tests/unit/test_one_off_vendor_retirement_lifecycle.py ........................
tests/unit/test_odoo_supplier_partner_writer.py ........................
112 passed
```

`ruff check` and `ruff format --check` both pass on the new file. No other repository files
were modified. No draft PR was opened in this task — per the task's own instruction ("Do NOT
implement them during 09A" for Phase 10 slices) and because the ONE_OFF_VENDOR reuse gap (G1)
requires a real design decision (see P0-PROD-09C's non-goals) rather than a pure mechanical
fix, it is correctly scoped as a follow-up, not a same-task correction.

## SAFETY

Production writes this task: 0. Odoo writes: 0. Hub business writes: 0. Gate changes: 0
(gates were only *read*, never modified). Decisions: 0. Executions: 0. Vendor Bills: 0.
Partner changes: 0. Product changes: 0. Supplierinfo changes: 0. Expense mappings: 0.
Uyumsoft mutations: 0. No secrets were printed or logged.
