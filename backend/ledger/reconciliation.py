"""Detection where prevention does not fit (ADR-0026).

Three of the ledger's four invariants are enforced by Postgres at COMMIT: entries balance (0002),
shares are conserved per instrument (0009), and a line moves something. The fourth — *no asset
account ever goes negative* — cannot be, and the reasons are worth stating because "just add another
trigger" is the obvious wrong answer:

* A deferred constraint trigger's ``SELECT SUM(amount)`` runs on a snapshot that cannot see other
  uncommitted transactions. Two concurrent withdrawals of 80 against a balance of 100 each see a
  legal result and both commit. It would catch the violation *sometimes*, depending on commit
  interleaving — and the flakiness would be in the passing direction, which is worse than a
  documented gap.
* Locking inside the trigger does not rescue it: constraint triggers must be ``FOR EACH ROW``, so
  lock acquisition order becomes row-insert order, which is precisely the ad-hoc ordering ADR-0010's
  ``lock_accounts`` exists to eliminate.
* And it would cost ``O(the account's lifetime line count)`` on every write forever, against
  ``assert_entry_balanced``'s two-to-four rows.

So the overdraft rule stays where ADR-0010 put it — enforced by every writer taking the lock first —
and this module is the backstop: a scan over *committed* data, where the snapshot problem does not
exist, run on a schedule. Detection rather than prevention, which is what the invariant admits.
"""

from dataclasses import dataclass
from typing import Any

from .share_conservation import find_unconserved_entries


@dataclass(frozen=True)
class Violation:
    """One broken invariant, named precisely enough to go and look at the row."""

    invariant: str
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"{self.invariant}: {self.subject} ({self.detail})"


def _unbalanced_entries(connection: Any) -> list[Violation]:
    """Entries whose amounts do not sum to zero. Should be impossible since Week 2's trigger."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT entry_id, SUM(amount) AS net
            FROM ledger_journalline
            GROUP BY entry_id
            HAVING SUM(amount) <> 0
            """
        )
        return [
            Violation("unbalanced_entry", str(entry_id), f"amounts net to {net}")
            for entry_id, net in cursor.fetchall()
        ]


def _negative_asset_balances(connection: Any) -> list[Violation]:
    """Asset accounts in overdraft — the invariant no trigger can hold.

    Scoped to ASSET on purpose. The ``Opening balances`` equity account that funds every account is
    negative by construction, and so is a ``Realized P&L`` income account after a profitable sell:
    both are the *source* of money that exists elsewhere. A check that reported them would cry wolf
    on every healthy ledger, which is the fastest way to make a report nobody reads.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.id, SUM(l.amount) AS balance
            FROM accounts_account a
            JOIN ledger_journalline l ON l.account_id = a.id
            WHERE a.account_type = 'asset'
            GROUP BY a.id
            HAVING SUM(l.amount) < 0
            """
        )
        return [
            Violation("negative_asset_balance", str(account_id), f"balance {balance}")
            for account_id, balance in cursor.fetchall()
        ]


def _negative_holdings(connection: Any) -> list[Violation]:
    """Position accounts holding fewer than zero shares — an accidental short (ADR-0018)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.id, SUM(l.quantity) AS held
            FROM accounts_account a
            JOIN ledger_journalline l ON l.account_id = a.id
            WHERE a.instrument_id IS NOT NULL AND a.account_type = 'asset'
            GROUP BY a.id
            HAVING SUM(l.quantity) < 0
            """
        )
        return [
            Violation("negative_holding", str(account_id), f"{held} shares")
            for account_id, held in cursor.fetchall()
        ]


def check_invariants(connection: Any) -> list[Violation]:
    """Every violation currently in the ledger, cheapest-to-hardest.

    The share-conservation scan is shared with the migration that created the trigger — the same
    query, asked of the whole table. Reusing it is deliberate: a reconciliation report that
    disagreed with the constraint it is reconciling against would be worse than none.
    """
    return [
        *_unbalanced_entries(connection),
        *[
            Violation("unconserved_shares", entry_id, f"instrument {instrument_id} net {net}")
            for entry_id, instrument_id, net in find_unconserved_entries(connection)
        ],
        *_negative_asset_balances(connection),
        *_negative_holdings(connection),
    ]
