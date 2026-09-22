#!/usr/bin/env bash
# ICT Integration Hub -- production PostgreSQL backup.
#
# Runs pg_dump inside the running production `db` container (custom format,
# compressed), using the exact POSTGRES_DB / POSTGRES_USER the container
# itself is already configured with -- never a separate env file. The
# PostgreSQL password is never read, exported, or printed by this script:
# pg_dump runs *inside* the db container via `docker compose exec`, which
# authenticates locally (the container's own POSTGRES_PASSWORD_FILE /
# docker secret), so no password ever needs to cross this script at all.
#
# See docs/BACKUP_RESTORE.md for backup verification and the restore
# procedure. This script only ever creates or deletes files matching its
# own "${BACKUP_PREFIX}_*.dump" pattern inside BACKUP_DIR -- it never
# touches any other file there.
set -euo pipefail

COMPOSE_DIR="/opt/ict-integration-hub/config"
COMPOSE_FILE="docker-compose.prod.yml"
DB_SERVICE="db"
BACKUP_DIR="/opt/ict-integration-hub/backups"
BACKUP_PREFIX="ict_integration_hub_prod"
RETENTION_DAYS=14

compose() {
  docker compose -f "${COMPOSE_DIR}/${COMPOSE_FILE}" "$@"
}

# --- Preconditions -----------------------------------------------------

if ! compose ps --status running --services 2>/dev/null | grep -qx "${DB_SERVICE}"; then
  echo "BACKUP FAILED: the '${DB_SERVICE}' service is not running under ${COMPOSE_FILE}." >&2
  exit 1
fi

POSTGRES_DB="$(compose exec -T "${DB_SERVICE}" printenv POSTGRES_DB 2>/dev/null || true)"
if [ -z "${POSTGRES_DB}" ]; then
  echo "BACKUP FAILED: could not determine POSTGRES_DB from the running '${DB_SERVICE}' container." >&2
  exit 1
fi

POSTGRES_USER="$(compose exec -T "${DB_SERVICE}" printenv POSTGRES_USER 2>/dev/null || true)"
if [ -z "${POSTGRES_USER}" ]; then
  echo "BACKUP FAILED: could not determine POSTGRES_USER from the running '${DB_SERVICE}' container." >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
umask 077

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/${BACKUP_PREFIX}_${TS}.dump"

if [ -e "${OUT}" ]; then
  echo "BACKUP FAILED: ${OUT} already exists; refusing to overwrite." >&2
  exit 1
fi

# --- Backup --------------------------------------------------------------
# Custom format (-Fc), compressed (-Z 6): matches the format already in use
# for every existing production backup file, gives pg_restore --list /
# selective-restore capability, and is the format docs/BACKUP_RESTORE.md
# documents pg_restore against. Never changed merely for style.

compose exec -T "${DB_SERVICE}" pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc -Z 6 > "${OUT}.tmp"
mv "${OUT}.tmp" "${OUT}"
chmod 600 "${OUT}"

if [ ! -s "${OUT}" ]; then
  echo "BACKUP FAILED: ${OUT} is empty." >&2
  rm -f "${OUT}"
  exit 1
fi

BACKUP_SIZE="$(stat -c %s "${OUT}" 2>/dev/null || stat -f %z "${OUT}")"
echo "backup ok: ${OUT} (${BACKUP_SIZE} bytes)"

# --- Retention -------------------------------------------------------------
# Deletes only this script's own backup files, only under BACKUP_DIR itself
# (-maxdepth 1, no recursion), only when older than RETENTION_DAYS, and only
# when the exact "${BACKUP_PREFIX}_*.dump" name pattern matches -- never a
# broad or unscoped rm/find. Any other file in BACKUP_DIR (e.g. unrelated
# manual .env.*.bak snapshots) is never touched by this line.
find "${BACKUP_DIR}" -maxdepth 1 -type f -name "${BACKUP_PREFIX}_*.dump" -mtime "+${RETENTION_DAYS}" -print -delete
