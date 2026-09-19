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

# Which stack to act on. The defaults are the production compose file and its `.env`, which is what
# the cron entry and the runbook use. They are overridable for one reason: the restore drill below is
# only worth anything if it has actually been rehearsed, and rehearsing it on a laptop means pointing
# at the CI-shaped stack `make up` brings up, where `deploy/.env` does not exist.
#
#     ENV_FILE=deploy/.env.ci COMPOSE_OVERRIDE=deploy/compose.ci.yml \
#       ./deploy/restore.sh /tmp/banking-backups/db-<stamp>.dump.gpg
ENV_FILE="${ENV_FILE:-deploy/.env}"
COMPOSE=(docker compose -f deploy/compose.yml)
# An `if` rather than `[[ ... ]] && ...`: under `set -e` the short-circuit form evaluates to 1 when
# the variable is unset, which is harmless here only because another statement follows it.
if [[ -n "${COMPOSE_OVERRIDE:-}" ]]; then
    COMPOSE+=(-f "$COMPOSE_OVERRIDE")
fi
COMPOSE+=(--env-file "$ENV_FILE")

# shellcheck disable=SC1091
source "$ENV_FILE"

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

# Row counts prove the dump *arrived*. They do not prove it arrived **consistent** — a dump taken
# while the application is writing can land mid-transaction, and the shape of that damage is
# precisely what this ledger's invariants describe: an entry whose amounts no longer sum to zero,
# an instrument whose shares no longer net, a holding gone negative. Running the real checker
# against the scratch copy is the difference between "the file restored" and "the data is sound",
# and it costs one container.
#
# `--no-deps` because the dependencies are already up, and without it compose would also start the
# one-shot `migrate` service against the *live* database, which is not what a restore drill should
# touch.
#
# **Run as `migrate`, not as `app_blue`, and that is not cosmetic.** The two replicas hold pinned
# addresses so nginx can name them (ADR-0043), and a one-off `compose run` inherits the address of
# the service it runs as. With the stack up, that address is already taken by the running replica
# and the container dies with `failed to set up container networking: Address already in use`. The
# `migrate` service is the same image with the same environment and no pinned address, so it is the
# one that can be started alongside a live stack. Discovered when this check failed during the
# first end-to-end drill through S3.
#
# Set VERIFY_INVARIANTS=0 to skip, for a recovery where the application image is not to hand.
if [[ "${VERIFY_INVARIANTS:-1}" == "1" ]]; then
    echo "→ checking ledger invariants on the restored copy"
    "${COMPOSE[@]}" run --rm --no-deps \
        -e "DATABASE_URL=${DATABASE_URL%/*}/${TARGET}" \
        migrate python manage.py check_ledger_invariants
fi

echo
echo "Compare those against production. If they match, the backup is real."
echo "Drop the scratch copy when you are satisfied:"
echo "  docker compose -f deploy/compose.yml exec postgres psql -U ${POSTGRES_USER:-banking} -d postgres -c 'DROP DATABASE ${TARGET};'"
