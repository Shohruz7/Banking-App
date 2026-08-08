"""Making share conservation true of history, and checkable forever after (ADR-0025).

Three functions, all taking the ``apps`` registry or a raw connection so migrations can call them
with historical models — and all living here rather than inside a migration module, because
``coverage.omit`` excludes ``*/migrations/*`` and this is the code in Week 7 that rewrites rows
nobody is watching.

:func:`find_unconserved_entries` outlives its migration. It is the same query the deferred trigger
runs, asked of the whole table instead of one entry, which makes it both the migration's proof that
the backfill worked and the reconciliation command's proof that it has stayed worked.
"""

from typing import Any

#: Username of the account-owner that stands in for the market. Kept in step with
#: ``ledger.services.SYSTEM_USERNAME``; duplicated rather than imported because a migration must not
#: depend on the current shape of the service layer.
SYSTEM_USERNAME = "system"


def ensure_share_contras(apps: Any) -> int:
    """Create the system user and one shares-outstanding account per traded instrument.

    Only instruments that have actually been held get one. An instrument nobody ever bought needs no
    contra until the fill that creates it, which is the same laziness ``position_account_for`` uses.
    """
    User = apps.get_model("auth", "User")
    Account = apps.get_model("accounts", "Account")
    JournalLine = apps.get_model("ledger", "JournalLine")

    system_user, _ = User.objects.get_or_create(
        username=SYSTEM_USERNAME,
        defaults={"is_active": False, "email": "", "password": "!"},
    )

    # `.positions()` is unavailable here: custom managers are not carried into historical models
    # unless `use_in_migrations` is set, so this spells the filter out by hand.
    traded = (
        JournalLine.objects.filter(
            quantity__isnull=False,
            account__instrument__isnull=False,
            account__account_type="asset",
        )
        .values_list("account__instrument_id", flat=True)
        .distinct()
    )

    created = 0
    for instrument_id in traded:
        instrument = apps.get_model("markets", "Instrument").objects.get(pk=instrument_id)
        _, was_created = Account.objects.get_or_create(
            owner=system_user,
            instrument_id=instrument_id,
            account_type="equity",
            defaults={"name": f"{instrument.symbol} shares outstanding", "currency": "USD"},
        )
        created += int(was_created)
    return created


def backfill_contra_lines(connection: Any) -> int:
    """Give every historical fill the contra leg it was posted without.

    Raw SQL rather than ``bulk_create`` for one specific reason: ``JournalLine.created_at`` is
    ``auto_now_add``, which overrides whatever the caller sets, and stamping these rows with
    migration time would quietly falsify the promise ``accounts.models`` makes about ``as_of``
    queries — *"lines are never backdated, so the answer for a closed period is stable no matter
    when it is asked"*. A July statement has to keep saying what it said. So the contra copies its
    sibling's timestamp.

    Aggregated per ``(entry, instrument)`` rather than per line, so an entry that touched one
    holding twice gets one contra for the net, and so re-running inserts nothing.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger_journalline
                (entry_id, account_id, amount, quantity, currency, created_at)
            SELECT agg.entry_id, contra.id, 0, -agg.net, 'USD', agg.posted_at
            FROM (
                SELECT l.entry_id,
                       held.instrument_id,
                       SUM(l.quantity) AS net,
                       MIN(l.created_at) AS posted_at
                FROM ledger_journalline l
                JOIN accounts_account held ON held.id = l.account_id
                WHERE l.quantity IS NOT NULL
                  AND held.instrument_id IS NOT NULL
                  AND held.account_type = 'asset'
                GROUP BY l.entry_id, held.instrument_id
            ) agg
            JOIN accounts_account contra
              ON contra.instrument_id = agg.instrument_id
             AND contra.account_type = 'equity'
            WHERE agg.net <> 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM ledger_journalline existing
                  JOIN accounts_account a ON a.id = existing.account_id
                  WHERE existing.entry_id = agg.entry_id
                    AND a.instrument_id = agg.instrument_id
                    AND a.account_type = 'equity'
              )
            """
        )
        return int(cursor.rowcount)


def find_unconserved_entries(connection: Any) -> list[tuple[str, str, str]]:
    """Every ``(entry_id, instrument_id, net)`` whose shares do not net to zero.

    The trigger's query without the ``WHERE entry_id =`` — which is exactly why it is worth having
    separately. ``CREATE CONSTRAINT TRIGGER`` takes no ``NOT VALID`` and runs no validation scan, so
    creating the trigger proves nothing about rows already in the table. This is the scan Postgres
    declines to run.

    The ``NULL`` instrument group is deliberately not filtered out: it collects any quantity sitting
    on a *cash* line, which is forbidden by ``post_entry`` and by nothing in the database.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT l.entry_id, a.instrument_id, SUM(l.quantity) AS net
            FROM ledger_journalline l
            JOIN accounts_account a ON a.id = l.account_id
            GROUP BY l.entry_id, a.instrument_id
            HAVING COALESCE(SUM(l.quantity), 0) <> 0
            ORDER BY l.entry_id
            """
        )
        return [(str(row[0]), str(row[1]), str(row[2])) for row in cursor.fetchall()]
