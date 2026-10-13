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
