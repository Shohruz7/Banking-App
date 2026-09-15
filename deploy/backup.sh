#!/usr/bin/env bash
#
# Nightly backup: the database, and the statement PDFs beside it.
#
#     0 3 * * *  /srv/banking/deploy/backup.sh >> /var/log/banking-backup.log 2>&1
#
# **The media half is not an afterthought — it is the reason ADR-0039 could decline S3.** Keeping
# statements on a local volume is only defensible if they leave the box on a schedule; without this
# script, "we chose FileSystemStorage" would just mean "we chose one disk".
#
# `pg_dump -Fc` (custom format) rather than plain SQL: it compresses, and `pg_restore` can restore
# selectively from it, which is what you want at 3am when one table is wrong.
#
# Encrypted before it leaves, because a dump contains every account number and TOTP secret in the
# system. They are encrypted at rest in the column (ADR-0027), but the KEK lives in the environment
# of the machine being backed up, so treating the dump as sensitive is the only safe assumption.

set -euo pipefail

cd "$(dirname "$0")/.."

# Which stack to act on. The defaults are the production compose file and its `.env`, which is what
# the cron entry and the runbook use. They are overridable for one reason: the restore drill below is
# only worth anything if it has actually been rehearsed, and rehearsing it on a laptop means pointing
# at the CI-shaped stack `make up` brings up, where `deploy/.env` does not exist.
#
#     ENV_FILE=deploy/.env.ci COMPOSE_OVERRIDE=deploy/compose.ci.yml \
#       BACKUP_DIR=/tmp/banking-backups ./deploy/backup.sh
ENV_FILE="${ENV_FILE:-deploy/.env}"
COMPOSE=(docker compose -f deploy/compose.yml)
# An `if` rather than `[[ ... ]] && ...`: under `set -e` the short-circuit form evaluates to 1 when
# the variable is unset, which is harmless here only because another statement follows it.
if [[ -n "${COMPOSE_OVERRIDE:-}" ]]; then
    COMPOSE+=(-f "$COMPOSE_OVERRIDE")
fi
COMPOSE+=(--env-file "$ENV_FILE")

# Captured before sourcing, so a value passed on the command line beats one baked into the env file.
# Without this, `BACKUP_DIR=/tmp/x ./deploy/backup.sh` would silently write wherever `.env` said.
_BACKUP_DIR_OVERRIDE="${BACKUP_DIR:-}"

# shellcheck disable=SC1091
source "$ENV_FILE"

BACKUP_DIR="${_BACKUP_DIR_OVERRIDE:-${BACKUP_DIR:-/var/backups/banking}}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

# `+%FT%TZ` rather than `-uIs`: the latter is a GNU extension and prints an error on BSD date,
# which matters now that this script is meant to be rehearsed on a laptop before it is trusted.
echo "[$(date -u +%FT%TZ)] backup ${STAMP} starting"

# `-T` because there is no TTY in cron, and the dump would otherwise arrive with \r line endings —
# a corruption that only shows up when you try to restore it.
"${COMPOSE[@]}" exec -T postgres \
    pg_dump -U "${POSTGRES_USER:-banking}" -d "${POSTGRES_DB:-banking}" -Fc \
    > "${BACKUP_DIR}/db-${STAMP}.dump"

"${COMPOSE[@]}" exec -T app tar -czf - -C /app media \
    > "${BACKUP_DIR}/media-${STAMP}.tar.gz"

if [[ -n "${BACKUP_PASSPHRASE:-}" ]]; then
    # Armed before the first gpg call and cleared after the last. Between those two points the
    # plaintext dump — every account number and TOTP secret in the system — is on disk, and every
    # way encryption fails (gpg missing, full disk, unreadable passphrase) aborts under `set -e`
    # with those files still sitting there. The happy path removes them itself; this is for the
    # other paths. Discovered on the first real run of this script, where gpg was not installed.
    trap 'rm -f "${BACKUP_DIR}/db-${STAMP}.dump" "${BACKUP_DIR}/media-${STAMP}.tar.gz"' EXIT
    for file in "${BACKUP_DIR}/db-${STAMP}.dump" "${BACKUP_DIR}/media-${STAMP}.tar.gz"; do
        gpg --batch --yes --symmetric --cipher-algo AES256 \
            --passphrase "$BACKUP_PASSPHRASE" -o "${file}.gpg" "$file"
        rm "$file"
    done
    trap - EXIT
    echo "  encrypted"
else
    echo "  WARNING: BACKUP_PASSPHRASE unset — these dumps are plaintext PII." >&2
fi

# Off the box, or it is not a backup. The one thing a local copy cannot survive is losing the box,
# which is the scenario this exists for.
if [[ -n "${BACKUP_S3_URI:-}" ]]; then
    aws s3 cp "${BACKUP_DIR}/" "${BACKUP_S3_URI}/" --recursive \
        --exclude '*' --include "*-${STAMP}.*"
    echo "  shipped to ${BACKUP_S3_URI}"
else
    echo "  WARNING: BACKUP_S3_URI unset — this backup has not left the machine." >&2
fi

find "$BACKUP_DIR" -type f -mtime "+${RETAIN_DAYS}" -delete
echo "[$(date -u +%FT%TZ)] backup ${STAMP} done"
