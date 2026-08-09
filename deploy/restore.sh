#!/usr/bin/env bash
#
# Restore a dump into a scratch database and count what arrived.
#
#     ./deploy/restore.sh /var/backups/banking/db-20260809T030000Z.dump.gpg
#
# **Run this once, deliberately, before you need it.** An untested backup is a wish: the failure
# modes — a dump truncated by a full disk, a gpg passphrase nobody recorded, a pg_dump that has been
# writing zero bytes since a container rename — are all silent, and all only visible on restore.
#
# Restores into a *scratch* database by default rather than over the live one. Recovering for real
# is the same `pg_restore` with `--dbname` pointed at production, and that should be a decision
# somebody makes at a keyboard, not something a script does because it was invoked.

set -euo pipefail

ARCHIVE="${1:-}"
if [[ -z "$ARCHIVE" ]]; then
    echo "usage: $0 <dump-file[.gpg]> [target-db]" >&2
    exit 2
fi

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f deploy/compose.yml --env-file deploy/.env)
# shellcheck disable=SC1091
source deploy/.env

TARGET="${2:-banking_restore_check}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ "$ARCHIVE" == *.gpg ]]; then
    : "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE must be set to decrypt this archive}"
    gpg --batch --yes --decrypt --passphrase "$BACKUP_PASSPHRASE" \
        -o "${WORK}/db.dump" "$ARCHIVE"
else
    cp "$ARCHIVE" "${WORK}/db.dump"
fi

echo "→ restoring into ${TARGET} (scratch)"
"${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:-banking}" -d postgres \
    -c "DROP DATABASE IF EXISTS ${TARGET};" -c "CREATE DATABASE ${TARGET};"

"${COMPOSE[@]}" exec -T postgres pg_restore -U "${POSTGRES_USER:-banking}" \
    --dbname "$TARGET" --no-owner --no-privileges < "${WORK}/db.dump"

echo "→ what came back"
"${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:-banking}" -d "$TARGET" -c "
SELECT 'users'          AS table, count(*) FROM auth_user
UNION ALL SELECT 'accounts',        count(*) FROM accounts_account
UNION ALL SELECT 'journal entries', count(*) FROM ledger_journalentry
UNION ALL SELECT 'journal lines',   count(*) FROM ledger_journalline
UNION ALL SELECT 'orders',          count(*) FROM trading_order
UNION ALL SELECT 'audit events',    count(*) FROM audit_auditevent;
"

echo
echo "Compare those against production. If they match, the backup is real."
echo "Drop the scratch copy when you are satisfied:"
echo "  docker compose -f deploy/compose.yml exec postgres psql -U ${POSTGRES_USER:-banking} -d postgres -c 'DROP DATABASE ${TARGET};'"
