# ADR-0014: Advisory Product Suggestions (PR F)

- Status: Proposed
- Date: 2026-10-13
- Related: [ADR-0004](0004-odoo-resolution-engine.md) (exact resolution only), [ADR-0011](ADR-0011-odoo-online-import-workbench-projection.md), [ADR-0013](ADR-0013-odoo-online-workbench-operator-requests.md)

## Context

When a line is PRODUCT_NOT_FOUND, the operator searches the Odoo catalog by hand before choosing
*Eşleştir* or *Yeni Ürün Oluştur*. The architecture excludes name similarity, fuzzy matching and
AI matching from **automatic** matching (ADR-0004, `ARCHITECTURE.md`), and the Workbench guard
tests keep those techniques out of `app/application/workbench` entirely. An operator-facing
shortlist is allowed by the architecture as an *advisory recommendation*, but it needs a place
that cannot leak into any deciding code path.

## Decision

1. Advisory product suggestions live in the dedicated package `app/advisory`, outside the
   Workbench contracts and outside deterministic matching. The existing guards are unchanged.
2. The engine (`app/advisory/product_suggestions.py`) is pure: immutable snapshots in, a
   structured result out; no Odoo call, no database, no clock, no write.
3. Tiers T0 stale supplierinfo > T1 exact global identifier > T2 earlier same-supplier Hub mapping
   > T3 guarded name similarity. Hard discriminator guards (product family, edition, duration,
   capacity/metric, license model, conflicting manufacturer identifier) reject candidates; ties
   stay ties; at most 3 suggestions; no safe candidate means none.
4. Nothing in matching, decisions, execution, billing, the Workbench contracts or any ERP write
   path may import `app.advisory` (boundary test). A suggestion never selects, maps or creates a
   product, never writes supplierinfo or an Internal Reference, and never changes the match state;
   the operator's explicit *Eşleştir* request is validated exactly as before.

## Consequences

- F1 delivers only the engine and tests. Persistence, the Odoo catalog read, projection fields and
  Studio UI are later, separately approved stages (F2–F4) behind a default-off flag.
- A T0 result means existing evidence is stale; a controlled reclassification is a separate,
  explicitly approved action and is never triggered by the engine.

## F2 (persistence and worker)

- Tables `workbench_product_line_suggestion_sets` / `_candidates` (Alembic `202607170041`); a partial
  unique index allows one `current` set per (company, review, version, line); superseded and
  invalidated sets are history, removed after 90 days only by the dry-run-first CLI.
- An independent worker task (`advisory_product_suggestions`, every 15 minutes, behind
  `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_ENABLED`, default off) with a PostgreSQL advisory lock.
  It selects lines from committed Hub evidence exactly as the child projection does (effective
  commercial supplier of the persisted matching), reads the Odoo catalog in three bounded read-only
  calls, and never runs inside any other workflow.
- A set is displayable only while its last successful catalog validation is at most 20 minutes old;
  a failed catalog read regenerates and revalidates nothing.
- The import boundary allows exactly the advisory composition, service, repository, catalog reader
  and CLI to import `app.advisory`, and only the Hub worker and the CLI to wire it.

## F3.1 (display adapter, code only)

Studio plan, staleness contract, enablement order and rollback: [ODOO_WORKBENCH_PRODUCT_SUGGESTIONS.md](../ODOO_WORKBENCH_PRODUCT_SUGGESTIONS.md).

- Persisted suggestions reach the Workbench child lines only through
  `app/composition/advisory_display.py` (allowlisted, imports no other `app.composition` module so
  `app.composition.imports` can compose it without a cycle). The Workbench projection stays pure: it
  receives ERP-neutral `ProductLineSuggestionView`s and never ranks, gates or reads advisory data.
- Separate flag `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED` (default off; requires the
  F2 worker flag and child line projection). Off: no advisory row is read by the projection.
- Display gate (every projection, from every flow): review pending, set `current`, same review
  version, line is `product_not_found`, same line key and number, same effective supplier and seller
  code, at least one candidate or advisory note, and validated within the **19-minute projection cutoff** (20-minute
  validity minus one 60 s sweep interval). Validity policy itself is unchanged.
- Two optional child fields, mapped together or not at all: a Many2many of `product.product`
  (written as one `[[6, 0, ids]]` command, compared as an id set — the parent publisher's proven
  path) and a plain-text ranked summary carrying each candidate's persisted reasons and warnings plus
  the set's notes. A notes-only set (near-miss code, ambiguous supplierinfo, CSP ProductId-only,
  multi-variant catalog) shows an empty Many2many and a warning-only summary. Unmapped, the publisher's reads and writes are byte-identical
  to PR B. Mapped with display off, or outside the gate, or on a deactivated row, both are cleared.
  Operator input fields (`x_studio_ipp_req_*`, `x_studio_ipp_new_*`) are never mapped or written.
- Alembic `202607170042`: nullable `candidates.product_name` (the candidate's `display_name` from the
  generating catalog read — a rename changes the catalog fingerprint and regenerates the set) and
  nullable `sets.display_synced_at` (start of the last successful Odoo refresh reflecting the set's
  display state).
- Refresh: after every advisory tick (post-tick refresh) and every 60 s (sweep task
  `advisory_suggestion_display_sweep`, same advisory lock), the Hub asks the advisory tables which
  sets may be shown differently in Odoo than the gate allows — never refreshed since a display-state
  change (created, retired while possibly shown, revalidated after being cleared), or past the cutoff
  and not refreshed since. No due set means no Odoo call. Due reviews (at most 25 per sweep) are
  re-synced through the canonical projection synchronizer; success sets `display_synced_at`
  (compare-and-set on status and `validated_at`); an already-cleared set is never refreshed again.
  A failed refresh is not marked and is retried (60 s backoff doubling to 5 min); it never touches
  `validated_at`.

### Staleness contract

The Hub never *publishes* an expired suggestion and clears expired ones from Odoo while the worker
and Odoo are operational. **It does not guarantee Odoo UI expiry during an outage**: if the worker
is down, saturated, or Odoo writes fail, the Odoo UI may keep the last published suggestions until
the next successful refresh. The F3 Studio UI therefore always shows the static warning:

> Öneriler bilgilendirme amaçlıdır. Güncellikleri değişebilir; eşleştirme öncesinde ürünü doğrulayın.
