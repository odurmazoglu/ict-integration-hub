# Uyumsoft Inbound Invoice Poller

The Hub polls Uyumsoft for incoming e-invoices on a fixed interval (default 180 s).
The Hub owns polling. Odoo never triggers it; Odoo only receives the existing
Workbench projection.

```
uyumsoft-inbound-poller (own container)
  └─ every UYUMSOFT_INBOUND_POLL_INTERVAL_SECONDS, under a PostgreSQL advisory lock
       UyumsoftInvoiceSyncWorkflow (Inbox only, bounded lookback)
         ├─ skip invoices whose import idempotency key already exists  (read-only)
         └─ UyumsoftCanonicalInvoiceImporter → ImportInvoiceUseCase
              → review commit → runtime WorkbenchProjectionSynchronizer (flag-gated)
```

## What is reused and what is added

The poller reuses the exact pipeline behind `POST /api/v1/sync/uyumsoft/invoices`,
composed by the same `build_uyumsoft_canonical_invoice_importer`. It does not
implement a second import, identity, projection or Odoo write path.

It adds the following:

| Concern | Mechanism |
| --- | --- |
| Single flight across processes | `pg_try_advisory_lock(-2752363236075536948)` on a dedicated AUTOCOMMIT connection, held for the whole cycle. If the lock is busy, the cycle logs `status=skipped_locked`, which is not an error. If the process crashes, PostgreSQL drops its connection and releases the lock. |
| No overlap within one process | Cycles run sequentially. A cycle that runs longer than the interval is followed immediately by the next one, never concurrently. |
| Skipping known invoices | `KnownInboundInvoiceChecker` looks for a receipt or review whose `idempotency_key` is exactly `uyumsoft:company:<id>:inbox:<identity>`, the key `import_idempotency_key` builds. A known invoice is not re-persisted, re-downloaded or re-resolved against Odoo. `ImportInvoiceUseCase`'s duplicate check and the unique constraints remain authoritative. |
| Per-invoice isolation | Expected failures are already safe outcomes. An unexpected exception on one invoice is logged by type only and counted as `failed`, and the rest of the page continues. |
| Watermark | None. Every cycle queries `[now − LOOKBACK_DAYS, now + 1 day]` on the Uyumsoft execution (invoice) date, so a late-delivered invoice is still found while it is inside the window. No migration and no polling-state table are needed. |
| First-run safety | `--preview` is a read-only dry run of the next cycle's exact window (see below). |
| Audit volume | A successful cycle that selected no invoice keeps no `uyumsoft_sync_runs` row; it is visible only as a log line. |

Polling never submits decisions, authorizes writes or triggers Vendor Bill
execution. A new invoice becomes a `pending_review` review, exactly as a manual
sync would create it.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `UYUMSOFT_INBOUND_POLL_ENABLED` | `false` | When false, the worker only logs `uyumsoft_inbound_poller_disabled` and idles. It makes no Uyumsoft, Odoo or database call. |
| `UYUMSOFT_INBOUND_POLL_INTERVAL_SECONDS` | `180` | 60–3600 |
| `UYUMSOFT_INBOUND_POLL_LOOKBACK_DAYS` | `10` | 1–30. Covers the 7-day e-invoice delivery allowance plus weekends. |
| `UYUMSOFT_INBOUND_POLL_PAGE_SIZE` | `100` | 1–100 |
| `UYUMSOFT_INBOUND_POLL_MAX_PAGES` | `10` | 1–10. If a cycle reaches `page_size × max_pages` invoices, it logs `uyumsoft_inbound_poll_window_truncated`. |

`UYUMSOFT_SYNC_EXECUTE_ENABLED` is an independent switch that gates only the manual
HTTP route. `ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED` still decides whether new
reviews are projected to Odoo.

## Preview (first-run safety)

```
python -m app.workers.uyumsoft_inbound_poller --preview
```

The preview works while `UYUMSOFT_INBOUND_POLL_ENABLED=false`. It runs the same
`UyumsoftInvoiceSyncWorkflow` listing as a cycle, with the same window, Inbox-only
direction and pagination (both use `inbound_poll_request`). Its skip predicate
classifies each invoice and then skips it, so the preview persists nothing,
downloads nothing, parses no UBL, makes no Odoo call and imports nothing. It takes
no lock and writes no sync-run row. Hub reads use `open_read_only_session`, which
is a PostgreSQL `READ ONLY` transaction.

It prints one line per invoice (`STATUS  ETTN  INVOICE_NUMBER  INVOICE_DATE  SENDER_VKN  TOTAL  CURRENCY`)
and a summary:

| Status | Meaning | Next cycle |
| --- | --- | --- |
| `ALREADY_KNOWN` | An import receipt or review already exists for the exact import idempotency key. | Skipped |
| `NEW` | The Hub has never seen this invoice (no metadata row). | Imported |
| `WOULD_IMPORT` | The Hub has seen it (metadata exists) but never imported it, for example after an earlier failure or a sync without import. | Imported (retried) |

`next_cycle_would_import = NEW + WOULD_IMPORT` is exactly the set the next cycle
sends into `ImportInvoiceUseCase`. Whether each import succeeds still depends on
the import itself, for example company resolution. If the output says
`truncated=true`, the list is incomplete. The exit code is non-zero when
Uyumsoft cannot be read.

## Audit rows (`uyumsoft_sync_runs`)

No code reads `uyumsoft_sync_runs`. It is a write-only execution summary, and the
manual route is its only other writer. A poll cycle keeps its row only when the
cycle is meaningful:

| Cycle | Row kept | Why |
| --- | --- | --- |
| Selected ≥ 1 invoice (new, retried, imported or failed) | Yes | The row records what was processed. |
| Uyumsoft failure (`ConnectorError`) | Yes (`status=failed`) | Unchanged from the manual route. |
| Succeeded and selected nothing (empty window, or every invoice already known) | No | With nothing selected, no metadata, document, review or receipt is written, so the sync-run row is the only pending write and is rolled back. The cycle is still logged by `uyumsoft_inbound_poll_finished … audit_recorded=False`. |
| `skipped_locked` or a lock failure | No | This is unchanged: no sync run was started. |

## Failure semantics

| Situation | Result |
| --- | --- |
| Uyumsoft unreachable or timing out (`ConnectorError`) | `status=failed`. The only write is a failed `uyumsoft_sync_runs` audit row: no invoice, review or Odoo change. The next cycle retries. |
| WSDL cannot be loaded (raw transport error from the existing client) | `status=failed`, the cycle is rolled back, and no audit row is kept. |
| One invoice fails (download, UBL, company resolution, import) | That invoice is logged with its outcome and counted as `failed`. The others continue, and the cycle ends `completed_with_errors`. The invoice is not "known", so it is retried every cycle while inside the lookback window. |
| Database unavailable when the lock is taken | `status=failed`, and nothing is read from Uyumsoft. |
| The poller container dies | The API is unaffected. The lock is released, and uncommitted work in the cycle is discarded. Reviews already committed stay committed. |

## Logs

The logs contain one line per event. They carry identifiers such as ETTN,
`review_id` and `company_id`, and never include UBL/XML, credentials or tokens.

- `uyumsoft_inbound_poll_started cycle_id=… from=… to=…`
- `uyumsoft_inbound_poll_invoice cycle_id=… invoice_identity=ettn:… status=… company_id=… review_id=…`
- `uyumsoft_inbound_poll_finished cycle_id=… status=completed|completed_with_errors|failed|skipped_locked discovered=… already_known=… imported=… review_created=… already_imported=… failed=… duration_ms=…`

## Known limitations

- An invoice that fails permanently (for example, it is addressed to an unknown
  company, or its UBL is malformed) is re-downloaded and re-attempted every cycle
  until it leaves the lookback window. This is bounded, and it is logged each time.
- Invoices older than the lookback window are never polled. Use the manual sync
  route to backfill them.
- An invoice that keeps failing is selected every cycle, so each of those cycles
  keeps an audit row until the invoice leaves the lookback window.
- The advisory lock guards against a second *poller*. A manual sync that runs at the
  same time is protected only by the existing idempotency and unique constraints,
  as it is today.

## Production deployment and enablement runbook

Deployment and enablement are separate operator steps, and each needs approval.

1. **Back up**: run `/opt/ict-integration-hub/scripts/backup-postgres.sh`.
2. **Deploy the code**: check out the merge SHA (detached) in `/opt/ict-integration-hub/app`,
   then recreate `api` as usual. There is no migration. Poller settings are absent,
   so polling stays off.
3. **Add the worker service** to `/opt/ict-integration-hub/config/docker-compose.prod.yml`.
   Copy the `api` service definition (same build, entrypoint, `env_file`, secrets,
   network, and **the document-storage volume**), then change these fields:
   - `command: ["python", "-m", "app.workers.uyumsoft_inbound_poller"]`
   - remove `ports` and the HTTP healthcheck
   - `restart: unless-stopped`, and exactly one instance (no `replicas`/`scale`)

   Confirm that the entrypoint ends with `exec "$@"`, so the command replaces
   uvicorn, and that it exports `DATABASE_URL` and `ODOO_API_KEY` from
   `/run/secrets`.
4. **Start it disabled**: run `docker compose -f docker-compose.prod.yml up -d --no-deps uyumsoft-inbound-poller`.
   The log must show `uyumsoft_inbound_poller_disabled`, and `api` health must be unchanged.
5. **Preview (mandatory gate, read-only)**: while polling is still disabled, run
   `docker compose -f docker-compose.prod.yml exec -T uyumsoft-inbound-poller python -m app.workers.uyumsoft_inbound_poller --preview`,
   using the same `sh -c` entrypoint-env pattern as other prod CLI runs if
   `DATABASE_URL` or the secrets are only set by the entrypoint.
   - Review every `NEW` / `WOULD_IMPORT` line. These are exactly the invoices the
     first cycle will send into `ImportInvoiceUseCase`, each creating a pending
     Hub review and a Workbench row.
   - `truncated` must be `false`.
   - If an invoice must **not** be imported, do not enable yet. Either import only
     the wanted ones first with the manual sync route's `invoice_ettn` allowlist,
     or start with a smaller `UYUMSOFT_INBOUND_POLL_LOOKBACK_DAYS` so that the
     unwanted invoices fall outside the window. They will re-enter the window if it
     is widened while they are still younger than the new lookback.
   - Save the preview output with the change record.
6. **Enable it**: re-run the preview immediately before this step and confirm it
   matches the reviewed list; only invoices that arrived since may differ. Then
   back up `.env.production`, set `UYUMSOFT_INBOUND_POLL_ENABLED=true` (interval
   180 is the default), and recreate **only** the poller container
   (`up -d --no-deps uyumsoft-inbound-poller`).
7. **Watch the first cycles**: in `uyumsoft_inbound_poll_finished`, the first
   cycle's `discovered − already_known` must equal the preview's
   `next_cycle_would_import`. On later cycles, `imported=0`,
   `already_known=discovered` and `audit_recorded=False` are expected unless new
   invoices arrived. A new review then appears in the Odoo Workbench through the
   runtime projection synchronizer.

**Rollback**: set `UYUMSOFT_INBOUND_POLL_ENABLED=false` and recreate the poller,
or `docker compose … stop uyumsoft-inbound-poller`. Reviews that were already
imported stay in the Hub, the same as reviews from a manual sync. Code rollback
means redeploying the previous SHA; there is no migration to revert.
