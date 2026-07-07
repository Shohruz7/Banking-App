"""select_for_update intent — a stub for the full concurrency suite that arrives with transfers.

Week 3 builds transfers with deterministic lock ordering and lost-update tests. Here we only
prove the primitive is wired correctly on Postgres: it requires a transaction, and it takes a
real row lock a second connection cannot jump.
"""

import psycopg
import pytest
from django.db import connection, transaction

from accounts.models import Account
from tests.factories import AccountFactory


@pytest.mark.django_db(transaction=True)
def test_select_for_update_requires_a_transaction() -> None:
    account = AccountFactory.create()

    # Outside atomic() there is no transaction to hold the lock, so Django refuses.
    with pytest.raises(transaction.TransactionManagementError):
        list(Account.objects.select_for_update().filter(pk=account.pk))


@pytest.mark.django_db(transaction=True)
def test_select_for_update_blocks_a_second_connection() -> None:
    account = AccountFactory.create()
    db = connection.settings_dict

    with transaction.atomic():
        # Connection 1 holds a row lock on the account.
        locked = Account.objects.select_for_update().get(pk=account.pk)
        assert locked.pk == account.pk

        # Connection 2 asking for the same row FOR UPDATE NOWAIT fails immediately.
        conn2 = psycopg.connect(
            dbname=db["NAME"],
            user=db["USER"],
            password=db["PASSWORD"],
            host=db["HOST"] or "localhost",
            port=db["PORT"] or 5432,
        )
        try:
            with conn2.cursor() as cursor, pytest.raises(psycopg.errors.LockNotAvailable):
                cursor.execute(
                    "SELECT 1 FROM accounts_account WHERE id = %s FOR UPDATE NOWAIT",
                    (str(account.pk),),
                )
        finally:
            conn2.rollback()
            conn2.close()
