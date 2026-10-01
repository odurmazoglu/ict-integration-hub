# Append-only source-identity correction

A review's `workbench_review_source_invoice_evidence` row is an insert-only
snapshot of the `InternalInvoice` the Hub classified. When a parser defect that
has since been fixed persisted a wrong value into that snapshot, the snapshot is
**never rewritten**. Instead, an audited row is appended to
`workbench_review_source_invoice_corrections`, together with a single review
version step `N -> N+1`.

The first and only supported case is `UBL_PARTY_TAX_IDENTIFIER_PR201`. Before
PR #201, the UBL parser stored a party's *first* `PartyIdentification/cbc:ID`
(any `schemeID`, e.g. `MERSISNO`) as its tax number instead of the typed VKN or
TCKN. Only `supplier.tax_number` may be corrected for that reason. Both values
are enforced by the application allow-list and by database CHECK constraints.

## Effective source evidence

`SqlAlchemyReviewSourceInvoiceEvidenceReader.get()` returns the *effective*
source invoice: the original snapshot with every correction for the review
overlaid in `to_version` order. A broken correction chain is a data-integrity
error. Every existing consumer therefore sees the corrected value through the
normal reader, with no bypass:

- reclassification;
- supplier remediation (all modes, including the
  `CREATE_PERMANENT_SUPPLIER` / `ONE_OFF_VENDOR` partner VAT and the
  `MATCH_EXISTING` exact-VAT validation);
- decisions;
- review evidence.

`get_original()` returns the uncorrected snapshot for audit.

## Preconditions (all reported, any failure refuses)

| check | meaning |
|---|---|
| `review_exists` | review exists for the company |
| `not_already_applied` | no correction from `expected_version` exists; otherwise the outcome is `ALREADY_APPLIED` with zero writes |
| `review_pending` | status is `pending_review` |
| `version_matches` | current version equals `expected_version` |
| `no_decision` | no `workbench_review_decisions` row |
| `no_write_authorization` | no `workbench_review_write_authorizations` row |
| `no_execution` | no workflow execution, execution source/billing evidence or quotation scenario evidence |
| `no_downstream_remediation` | no supplier resolution/effect, one-off retirement, accounting or purchase-purpose resolution, product claim/reservation, or billing evidence |
| `source_document_present` | exactly one stored UBL document, linked through the review's import identity key, and readable |
| `source_hash_matches` | SHA-256 of the stored bytes equals `invoice_documents.content_hash_sha256` |
| `reparse_succeeds` | the currently deployed UBL parser parses it |
| `source_evidence_present` | the review has immutable source evidence |
| `only_target_field_differs` | re-parsed invoice differs from the effective evidence **only** at the corrected field path; no difference at all means `NO_CHANGE` |
| `review_item_matches_source` | `workbench_review_items.supplier_tax_number` equals the evidence value |
| `historical_identifier_matches_reason` | replaying the pre-PR #201 rule on the same document yields exactly the persisted value, and the current rule yields a different one |
| `new_value_valid` | new value is a 10-digit VKN or an 11-digit TCKN |

The historical-identifier check replays the defective rule against the stored
document, so it does not depend on the wrong identifier's shape (MERSIS, plate,
trade registry and so on). No invoice, supplier or identifier is hard-coded.

## Transaction

One Hub transaction with a guarded `UPDATE` (pending, at `expected_version`, with
the corrected field still at its old value):

1. append the `workbench_review_source_invoice_corrections` row (field, old/new
   value, document id, document SHA-256, reason, `approved_by`);
2. update `workbench_review_items`: `supplier_tax_number`, the recalculated
   workflow, reasons and warnings, and `version = N+1`;
3. append `workbench_review_classification_evidence` for `N+1` (and execution
   evidence when the recalculated result is executable);
4. append a `workbench_review_reclassifications` row with trigger
   `source_identity_corrected`.

Classification is recalculated by the normal `EffectiveDecisionResolver` and the
production deterministic `DecisionEngine` over the corrected source. The version
always advances, even when the outcome is unchanged, because the effective
source changed. `SOURCE_IDENTITY_CORRECTED` is reserved: the generic
`ReclassifyReviewCommand` rejects it.

After the commit, the normal `WorkbenchProjectionSynchronizer` runs (when
`ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED`). A projection failure is reported
and never undoes the Hub commit. Repair a failed projection with
`app.cli.reconcile_workbench_projection`.

Nothing writes `res.partner`, `account.move`, decisions, authorizations,
executions, Vendor Bills or Studio metadata.

## Operator CLI

```bash
# dry-run (default): PostgreSQL READ ONLY session, Odoo read-only, zero writes
python -m app.cli.correct_review_source_identity --company 1 \
    --review 'review:<uuid>@1' --review 'review:<uuid>@1' --approved-by <operator>

# apply: only the listed reviews, each in its own transaction
python -m app.cli.correct_review_source_identity --company 1 \
    --review 'review:<uuid>@1' --approved-by <operator> --apply
```

Each review needs an explicit `@expected_version`. Each review is reported
separately with:

- every precondition;
- the old and new value and the version step;
- the expected Hub writes;
- the expected Workbench projection changes.

A failure or refusal on one review never affects another. Re-running `--apply`
for an already corrected review reports `ALREADY_APPLIED` and writes nothing.

Exit codes:

| code | meaning |
|---|---|
| `0` | every review was `WOULD_APPLY`, `APPLIED`, `ALREADY_APPLIED` or `NO_CHANGE` |
| `1` | any review was `REFUSED` or `ERROR` |
| `2` | configuration error |

## Rollback

- **Before any `--apply`:** revert the PR and run
  `alembic downgrade 202607170033`. This drops only the empty corrections table.
- **After an `--apply`:** restore the pre-apply database backup *first*, then
  revert the code and schema. Reverting the code alone would leave the review
  rows on the corrected VKN while the reader falls back to the original value.
  Never edit correction rows by hand.
