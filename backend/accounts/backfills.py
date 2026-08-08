"""One-time data moves for the accounts app, kept out of ``migrations/`` so coverage sees them."""

from typing import Any

from accounts import numbers
from common.crypto import encrypt


def assign_missing_numbers(apps: Any) -> int:
    """Give every pre-ADR-0027 customer account a number.

    Scoped exactly as ``Account.save`` scopes it — asset accounts with no instrument — so an
    account created before this migration and one created after are indistinguishable afterwards.
    Idempotent: a row that already has ciphertext is skipped, and re-running never re-numbers an
    account, which would be worse than not running at all.
    """
    Account = apps.get_model("accounts", "Account")

    assigned = 0
    stale = Account.objects.filter(
        instrument__isnull=True, account_type="asset", number_ciphertext=""
    )
    for account in stale:
        number = numbers.generate()
        account.number_ciphertext = encrypt(number)
        account.number_last4 = numbers.last4(number)
        account.save(update_fields=["number_ciphertext", "number_last4"])
        assigned += 1
    return assigned
