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
- Each non-skipped cycle writes one `uyumsoft_sync_runs` row (about 480 per day at 180 s).
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
5. **Preview (read-only)**: call `GET /api/v1/connectors/uyumsoft/inbox?from=<now−10d>&to=<now+1d>&page_size=100`
   and compare the result with the Hub reviews. Every Inbox invoice in that window
   that is not already imported **will be imported on the first cycle**. Confirm
   that this is intended before continuing.
6. **Enable it**: back up `.env.production`, set `UYUMSOFT_INBOUND_POLL_ENABLED=true`
   (interval 180 is the default), and recreate **only** the poller container
   (`up -d --no-deps uyumsoft-inbound-poller`).
7. **Watch the first cycles**: check `uyumsoft_inbound_poll_finished` for
   `status`, `imported` and `failed`. On the second cycle, `imported=0` and
   `already_known=discovered` are expected unless new invoices arrived.

**Rollback**: set `UYUMSOFT_INBOUND_POLL_ENABLED=false` and recreate the poller,
or `docker compose … stop uyumsoft-inbound-poller`. Reviews that were already
imported stay in the Hub, the same as reviews from a manual sync. Code rollback
means redeploying the previous SHA; there is no migration to revert.
