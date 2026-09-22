# PostgreSQL Backup & Restore Runbook

This is the authoritative procedure for backing up and restoring the ICT
Integration Hub production PostgreSQL database (`ict_integration_hub_prod`).

Scope: PostgreSQL only. Document storage backup is covered separately in the
[Backup And Restore Checklist](PRODUCTION_READINESS.md#backup-and-restore-checklist).

Background: the production host previously carried an un-versioned
`scripts/backup-postgres.sh` that sourced a `.env.postgres.production` file
that never existed, so every invocation of that script failed before it ever
ran `pg_dump`. `scripts/backup-postgres.sh` in this repository (P0-PROD-12D)
fixes that by reading `POSTGRES_DB`/`POSTGRES_USER` directly from the running
`db` container's own environment instead of a separate file. It has not yet
replaced the production copy of the script — that is a separate, explicitly
authorized deployment task.

## 1. Backup

Run `scripts/backup-postgres.sh` on the production host, from any working
directory, as a user with `docker` access:

```bash
/opt/ict-integration-hub/scripts/backup-postgres.sh
```

What it does:

- Confirms the `db` service is running under
  `/opt/ict-integration-hub/config/docker-compose.prod.yml`.
- Reads `POSTGRES_DB` and `POSTGRES_USER` from the running `db` container's
  own environment (`docker compose exec -T db printenv ...`) — never from a
  separate env file, never hard-coded.
- Runs `pg_dump -Fc -Z 6` **inside** the `db` container via
  `docker compose exec`. The PostgreSQL password is never read, exported, or
  printed by the script: `pg_dump` authenticates locally inside the
  container using its own already-configured credentials
  (`POSTGRES_PASSWORD_FILE` / the `hub_db_password` Docker secret).
- Writes to a temp file first, then atomically renames to
  `/opt/ict-integration-hub/backups/ict_integration_hub_prod_<UTC timestamp>.dump`
  (e.g. `ict_integration_hub_prod_20260922T090514Z.dump`), and `chmod 600`s it.
- Refuses to overwrite an existing file at that exact path.
- Fails with a clear message (and non-zero exit) if the `db` service isn't
  running, if `POSTGRES_DB`/`POSTGRES_USER` can't be determined, or if the
  resulting file is empty.
- Deletes only its own `ict_integration_hub_prod_*.dump` files older than 14
  days, only directly under the backup directory (no recursion) — it never
  touches any other file there (e.g. the unrelated manual `.env.*.bak`
  snapshots already present in that directory).
- Does not stop or restart any production container.

Format: PostgreSQL **custom format** (`-Fc`), compressed (`-Z 6`) — the same
format every existing backup file in production already uses and the format
`docs/PRODUCTION_READINESS.md`'s migration runbook already documents
`pg_restore --list` against. This is not a stylistic choice; changing it
would break compatibility with every backup already taken.

## 2. Backup Verification

After every backup, before trusting it:

```bash
# 1. Confirm the file exists and is non-empty (the script already asserts this,
#    but re-check independently before relying on an older backup).
ls -la /opt/ict-integration-hub/backups/ict_integration_hub_prod_<timestamp>.dump

# 2. Inspect the dump's table of contents without touching any database.
#    Run pg_restore FROM INSIDE the db container so no client-side pg_restore
#    version mismatch can occur.
docker compose -f /opt/ict-integration-hub/config/docker-compose.prod.yml \
  cp /opt/ict-integration-hub/backups/ict_integration_hub_prod_<timestamp>.dump db:/tmp/verify.dump
docker compose -f /opt/ict-integration-hub/config/docker-compose.prod.yml \
  exec -T db pg_restore --list /tmp/verify.dump
docker compose -f /opt/ict-integration-hub/config/docker-compose.prod.yml \
  exec -T db rm -f /tmp/verify.dump
```

A healthy dump's `pg_restore --list` output starts with an archive header
(`dbname:`, `Format: CUSTOM`, `Dumped by pg_dump version: ...`) followed by a
non-trivial list of `TABLE`/`TABLE DATA`/`CONSTRAINT`/`SEQUENCE` entries
covering the application's known tables (`workflow_executions`,
`workbench_review_items`, `alembic_version`, etc.). An empty or truncated
entry list, or a `pg_restore` error, means the backup is not usable — do not
proceed to a restore with it; re-run the backup and investigate instead.

`pg_restore --list` only reads the archive's table of contents. It performs
no database connection and changes no state.

## 3. Restore Preconditions

Restoring is a **destructive, operator-controlled action**. There is no
automatic restore path anywhere in this repository or on the production
host, and none should be added. Before restoring:

1. **Identify the intended backup file explicitly** by its exact timestamped
   filename — never "the newest file" by convention alone. Confirm the
   timestamp against the incident timeline (what state do you actually want
   to recover to?).
2. **Run backup verification (Section 2)** against that exact file. Do not
   restore a file that hasn't passed `pg_restore --list` inspection.
3. **Confirm the target environment.** Run `docker compose -f
   /opt/ict-integration-hub/config/docker-compose.prod.yml ps` and confirm
   the `db` container's `POSTGRES_DB` (`docker compose exec -T db printenv
   POSTGRES_DB`) matches the database you intend to restore into. Never
   restore into a database you have not explicitly identified by name.
4. **Take a fresh safety backup of the current state first**, where
   possible, using Section 1's procedure — even if the current database is
   believed to be corrupt or wrong, a fresh dump preserves the ability to
   compare or recover from a botched restore. Skip only if the current
   state is confirmed already lost/unreadable.
5. **Stop the application container** (`api`) before restoring, so no
   concurrent write reaches the database mid-restore:
   ```bash
   docker compose -f /opt/ict-integration-hub/config/docker-compose.prod.yml stop api
   ```
   Do not stop `db` itself — `pg_restore` needs it running. Do not touch
   `keycloak` or network/firewall configuration.
6. **Get explicit human authorization** for the specific restore action
   before proceeding to Section 4. This runbook documents the procedure; it
   does not authorize performing it.

## 4. Restore Procedure

Run every command against the exact backup file identified and verified in
Section 3.

```bash
COMPOSE="docker compose -f /opt/ict-integration-hub/config/docker-compose.prod.yml"

# 1. Copy the verified dump into the db container.
$COMPOSE cp /opt/ict-integration-hub/backups/ict_integration_hub_prod_<timestamp>.dump db:/tmp/restore.dump

# 2. Restore into the existing database, inside the container. --no-owner
#    --no-privileges avoids failing on role/ownership mismatches, since the
#    role names embedded in the dump are expected to already match this
#    deployment's POSTGRES_USER -- this flag pair only prevents unnecessary
#    ALTER OWNER / GRANT statements from being replayed, it does not change
#    what data is restored. --clean --if-exists drops existing objects
#    first so the restore is idempotent against the current schema state;
#    review its output for errors before proceeding.
$COMPOSE exec -T db pg_restore \
  -U <the POSTGRES_USER confirmed in Section 3, step 3> \
  -d <the POSTGRES_DB confirmed in Section 3, step 3> \
  --no-owner --no-privileges --clean --if-exists \
  /tmp/restore.dump

# 3. Remove the copied dump from the container filesystem.
$COMPOSE exec -T db rm -f /tmp/restore.dump
```

Never pass the PostgreSQL password on the command line, in an environment
variable dumped to logs, or in `docker compose exec`'s arguments — `pg_dump`/
`pg_restore` running inside the `db` container authenticate the same way the
container itself already does; no separate credential is required for either
command in this procedure.

## 5. Post-Restore Verification

Run all of the following before resuming traffic. Any failure here is an
abort condition (Section 6) — do not start `api` on an unverified database.

```bash
COMPOSE="docker compose -f /opt/ict-integration-hub/config/docker-compose.prod.yml"

# 1. PostgreSQL connectivity.
$COMPOSE exec -T db pg_isready -U <POSTGRES_USER> -d <POSTGRES_DB>

# 2. Alembic revision matches the expected head for the deployed application
#    version -- run this from a one-off container using the same image
#    already running in production, per the existing deployment pattern
#    documented in PRODUCTION_READINESS.md.
docker run --rm --network config_internal \
  --env-file /opt/ict-integration-hub/config/.env.production \
  -e PYTHONPATH=/app \
  -v /opt/ict-integration-hub/secrets/hub_db_password:/run/secrets/hub_db_password:ro \
  --entrypoint sh config-api:latest \
  -c 'export DATABASE_URL="postgresql+psycopg://<POSTGRES_USER>:$(cat /run/secrets/hub_db_password)@db:5432/<POSTGRES_DB>" && alembic current'

# 3. Start the application container.
$COMPOSE up -d api

# 4. Health checks.
curl -sS -o /dev/null -w 'live: %{http_code}\n' http://127.0.0.1:8000/health/live
curl -sS -o /dev/null -w 'ready: %{http_code}\n' http://127.0.0.1:8000/health/ready

# 5. Startup/error logs.
$COMPOSE logs api --tail=100
```

Expected outcomes: `pg_isready` reports `accepting connections`; `alembic
current` reports the exact revision expected for the deployed application
image (compare against the known head before the incident, not merely "some
revision"); `/health/live` and `/health/ready` both return `200`; the `api`
log tail shows no startup errors, no repeated connector/database exceptions,
and no unexpected restart.

## 6. Rollback / Abort Conditions

Abort the restore and escalate — do not improvise a fix — if any of the
following occurs:

- The identified backup file fails verification (Section 2): empty,
  truncated, or `pg_restore --list` errors.
- `pg_restore` (Section 4) exits non-zero, or its output shows errors beyond
  expected `DROP ... does not exist, skipping` notices from `--if-exists`.
- Alembic's post-restore revision (Section 5, step 2) does not match the
  expected revision for the deployed application image.
- `pg_isready`, `/health/live`, or `/health/ready` fail after starting
  `api` (Section 5).
- Application logs show repeated database/connector errors after restore.

If aborting after a restore has already been attempted:

1. Stop `api` again if it was started.
2. Do not attempt a second restore attempt improvisationally — re-verify the
   backup file and the exact command used before retrying.
3. If the database is now in a worse state than before the restore attempt,
   restore the **fresh safety backup** taken in Section 3, step 4, using
   this same procedure, to return to the last known-good pre-restore state.
4. Escalate to the technical owner before any further action. Do not resume
   `api` traffic on a database that failed post-restore verification.

Database rollback never removes Odoo records that were already created
against the pre-restore database state (existing caveat, unchanged from
`PRODUCTION_READINESS.md`'s Migration And Rollback section) — reconcile any
such records manually by ETTN/reference after a restore, exactly as for any
other rollback.

## Local Validation Performed For This Runbook

Every command sequence in Sections 1, 2, and 4 was exercised end-to-end
against disposable, local-only PostgreSQL 16 containers (never production)
before this runbook was written: a full backup → `pg_restore --list` →
drop/recreate database → `pg_restore --no-owner --no-privileges` → data
verification cycle completed successfully, and the backup script's
container-unavailable failure path, overwrite-refusal path, and retention
scoping (deletes only its own matching filenames, never unrelated files)
were each independently confirmed. No production database was accessed for
this validation. Section 3-6 restore-against-production and the full
post-restore verification sequence remain a separately authorized,
production-controlled task.
