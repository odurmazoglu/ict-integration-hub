# Controlled current-time review reclassification (P0-PROD-08O)

A pre-08M review can lack Stage-1 matching evidence even though its supplier
has been resolved and its only remaining reason is `PRODUCT_NOT_FOUND`. Exact
historical recovery is unsafe when the original matching results were never
persisted. The existing application reclassification operation can instead
create a **new current-time version**, using the immutable source invoice and
current deterministic matching inputs. It never fills the old version's missing
snapshot.

## Existing supported operation

Use `ReclassifyWorkbenchReviewUseCase.execute(ReclassifyReviewCommand(...))`
with an explicitly approved review identity, company, `expected_version`, the
existing `MASTER_DATA_CHANGED` trigger, and a note identifying the operation as
an explicit current-time matching refresh after the snapshot retention fix.
Do not label it supplier resolution or historical recovery.

The application boundary already accepts identity/version/trigger/note only;
no invoice, partner/product/tax selection, account-only decision, or arbitrary
evidence JSON is accepted. This is an internal application operation, not a REST
endpoint or a new CLI. There is no new HTTP permission or public operator
surface. An authorized maintenance caller must compose it directly and manage
the Hub transaction; no production invocation is authorized by this document.

Composition uses:

- `build_deterministic_decision_engine(session=..., settings=...)` from
  `app.composition.imports`, shared with normal import;
- `SqlAlchemyReviewSourceInvoiceEvidenceReader(session)`;
- `SqlAlchemyReviewRepository(session)` as the reclassification writer.

Do not invoke `ResolveWorkbenchSupplierUseCase` to reach this operation: that
orchestrator includes remediation writers, retirement handling, and optional
Odoo projection publishing. The direct reclassifier has none of these
capabilities. Do not compose execution or projection publishers. An eventual
operator surface or production invocation requires separate authorization.

## Why identical reasons can legitimately produce a new version

`SqlAlchemyReviewRepository.reclassify_review` treats a result as unchanged only
when all three are unchanged: workflow, serialized reasons, and presence of
Stage-1 execution evidence. The existing `executable` label here means **evidence
presence**, not permission or readiness to execute an unresolved line.

For a legacy v2, evidence presence is false. After 08M, a fresh deterministic
`MANUAL_REVIEW` result with a matched supplier, matched taxes and unresolved
product can carry partial evidence, so presence is true. That existing
comparison causes v2 to advance to v3 even when workflow/reasons are identical.
No forced-version flag, generic versioning change, new trigger, or migration is
needed. If all three are unchanged, the existing no-op remains unchanged.

The repository compare-and-set updates the pending projection from the exact
expected version, then inserts the transition event, classification evidence,
and Stage-1 matching evidence in the same savepoint. The enclosing caller must
commit or roll back the transaction. A failed snapshot insert rolls back the
projection and event. Evidence is pinned to version 3, never version 2.
The old event and source snapshot remain unchanged; the new event preserves
v2's workflow/reasons and records trigger, operator note and timestamp.

## Replay and concurrency

The unique `(review_id, from_version)` and `(review_id, to_version)` transition
constraints, evidence uniqueness, and projection compare-and-set prevent two
canonical v3 versions. An identical duplicate returns the existing transition;
a different deterministic transition result or lost version race fails closed.
This is the existing compare-and-confirm contract, rather than a new strict
reject-every-duplicate refresh contract. A concurrent identical request may
therefore safely confirm v3 instead of returning a conflict.

Always retain the original `expected_version=2` when retrying. Never read the
latest version and increment the request automatically. Even explicitly asking
for version 3 with identical workflow/reasons/evidence presence remains a no-op;
it cannot force v4. An unknown stale version fails closed.

The existing event fingerprint does not compare every nested matching field on
replay. A duplicate never inserts or replaces evidence; its response confirms
the original transition, not that current mutable matching DTOs are byte-for-byte
identical to its stored snapshot. Read the persisted v3 evidence to inspect the
canonical result. Do not treat replay as another matching snapshot refresh.

## Inputs and boundaries

The source invoice is persisted and immutable. Partner, product, tax and decision
rule candidates are current Odoo reads. Enabled operating-expense mappings are
current Hub reads and can influence the snapshot. This dependence is legitimate
for a **new current-time version**, never proof of historical equivalence.

The ONE_OFF_VENDOR ownership effect establishes who created the partner; normal
partner matching still requires a unique current active VAT match, and this is
unchanged for any review with no accepted remediation effect. Ownership,
retirement, supplier resolution and remediation effects are untouched. Product
lookup is read-only; no product or supplierinfo is created. If today's
candidates differ, review reasons/workflow may legitimately differ; a future
pilot invocation must check its expected supplier/product/tax state before
committing and roll back unexpected outcomes. No fixed tax or partner IDs are
supplied by the caller.

**P0-PROD-10D update:** when the raw deterministic partner match is *not*
matched (the case above always hits this for an archived partner, since
matching never considers inactive partners), and an accepted
`SupplierRemediationEffect` already exists for this exact `(review_id,
company_id)`, `ReclassifyWorkbenchReviewUseCase` now substitutes a synthesized
`MATCHED` partner (`matched_by="supplier_remediation_effect"`) for **Stage-1
execution evidence construction only** -- never for the classification
evidence or the reported `review_reasons`/`workflow`, which still reflect the
raw, honest matcher outcome. This lets a review whose supplier was legitimately
reused from an archived, Hub-owned ONE_OFF_VENDOR partner (see
`docs/ONE_OFF_VENDOR_RETIREMENT_RECOVERY.md` and PR #159) reach a submittable
decision without reactivating the partner and without any change to the
generic matcher. It is opt-in per composition root
(`supplier_remediation_effect_reader`, default `None`) and only ever reads the
one review's own accepted effect -- never another review's, another
company's, or an inactive partner with no recorded effect at all.

For the intended pilot-equivalent state, Stage 1 contains a matched supplier,
`PRODUCT_NOT_FOUND` for line 1 and a matched purchase VAT 20% tax. It contains no
operator `account_only`, `selected_product_id`, or selected `expense_account_id`.
The optional `account_only_expense_match` is a deterministic mapping result,
not an operator decision; absent mappings yield `NOT_FOUND`, with no account.

Only a later explicit `SubmitReviewDecisionUseCase` against v3 pins
`account_only=True` and `expense_account_id=247` into the accepted decision and
Stage-2 evidence. Acceptance advances the projection to decision version 4.
`VendorBillBuilder` can then consume that pinned account without a product ID.
The existing gross 805.01 / discount 241.50 normalization yields untaxed 563.51,
VAT 112.70 and payable 676.21. This document changes no discount logic.

## Validation and operational limits

The 08O tests in `test_account_only_execution_evidence_lifecycle.py` emulate the
old builder **at the original v1-to-v2 transition**, then run the real current
application use case, deterministic rule engine and SQLAlchemy repository. They
prove v3 snapshot insertion, immutable v2 history, replay/conflict handling,
transaction rollback and later in-memory account-only acceptance and bill build.
Matching readers and account validation are fixtures; these tests do not prove
that live production matching currently returns the same candidates.
Existing reclassification tests additionally cover version races, no-op results
and atomic rollback. Normal automatic behavior remains unchanged.

No runtime code, dependency or migration changes are introduced. Rollback is
reverting the documentation/tests commit. The real pilot remains untouched;
opening this validation PR does not authorize refreshing it, submitting a
decision, enabling gates, deploying, or creating an ERP document.
