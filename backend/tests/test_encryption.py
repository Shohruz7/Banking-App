"""Sensitive columns are ciphertext at rest (ADR-0027).

The finding this closes: ``MfaDevice.secret`` held a raw base32 TOTP shared secret, so a database
dump — a stolen backup, a replica, a `pg_dump` in a bucket — was a complete MFA bypass for every
enrolled user. That defeats an entire control rather than weakening one.

What it does *not* close: the KEK lives in the environment, so anything that can read the process
environment can still decrypt. This defends the dump, not the compromised app server. The seam is
the upgrade path.
"""

import base64
import os
from decimal import Decimal

import pyotp
import pytest
from django.contrib.auth.models import User
from django.db import connection
from pytest_django.fixtures import SettingsWrapper
from rest_framework.test import APIClient

from accounts import numbers
from accounts.models import Account, AccountType
from common.crypto import DecryptionError, decrypt, encrypt, is_encrypted
from identity.models import MfaDevice
from markets.models import Instrument

from .factories import AccountFactory, MfaDeviceFactory, UserFactory, fund_account

pytestmark = pytest.mark.django_db


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# ------------------------------------------------------------------------------------------------
# The primitive
# ------------------------------------------------------------------------------------------------


def test_a_value_round_trips() -> None:
    assert decrypt(encrypt("JBSWY3DPEHPK3PXP")) == "JBSWY3DPEHPK3PXP"


def test_the_same_plaintext_encrypts_differently_every_time() -> None:
    """A fresh data key and nonce per value, so equal secrets are not equal ciphertexts.

    Capable of failing against the tempting simplification of a single fixed data key, under which
    two users who happened to share a secret would be visibly identical in a dump — and every row's
    nonce reuse would eventually break GCM outright.
    """
    assert encrypt("same") != encrypt("same")


def test_an_empty_value_is_passed_through() -> None:
    """Absence is not a secret. Encrypting "" would make "no number yet" look like a real one."""
    assert encrypt("") == ""
    assert decrypt("") == ""


def test_a_tampered_ciphertext_is_refused(settings: SettingsWrapper) -> None:
    """AES-GCM authenticates; a flipped byte is detected rather than decrypted into rubbish."""
    settings.FIELD_ENCRYPTION_KEYS = {"k1": _key()}
    good = encrypt("secret")
    version, label, wrapped, nonce, _body = good.split(":")
    tampered = ":".join(
        [version, label, wrapped, nonce, base64.urlsafe_b64encode(b"nope").decode()]
    )

    with pytest.raises(DecryptionError):
        decrypt(tampered)


def test_a_ciphertext_from_an_unknown_key_is_refused(settings: SettingsWrapper) -> None:
    """Rotating a key out makes its rows unreadable — loudly, not as silent garbage."""
    settings.FIELD_ENCRYPTION_KEYS = {"old": _key()}
    ciphertext = encrypt("secret")
    settings.FIELD_ENCRYPTION_KEYS = {"new": _key()}

    with pytest.raises(DecryptionError):
        decrypt(ciphertext)


def test_rotation_reads_old_rows_and_writes_new_ones(settings: SettingsWrapper) -> None:
    """**The property the whole label scheme exists for.**

    Prepending a key must not orphan a single existing row. Capable of failing against the obvious
    single-``FIELD_ENCRYPTION_KEY`` design, under which rotating is a flag day: every row written
    under the old key becomes unreadable the moment the new one is deployed.
    """
    old, new = _key(), _key()
    settings.FIELD_ENCRYPTION_KEYS = {"v1": old}
    written_before = encrypt("older secret")

    settings.FIELD_ENCRYPTION_KEYS = {"v2": new, "v1": old}

    assert decrypt(written_before) == "older secret"  # old rows still readable
    written_after = encrypt("newer secret")
    assert written_after.split(":")[1] == "v2"  # new writes use the new key

    settings.FIELD_ENCRYPTION_KEYS = {"v2": new}
    assert decrypt(written_after) == "newer secret"  # and survive dropping the retired key


# ------------------------------------------------------------------------------------------------
# The TOTP secret
# ------------------------------------------------------------------------------------------------


def test_the_totp_secret_is_ciphertext_in_the_database() -> None:
    """Read the column with raw SQL, because the ORM would helpfully decrypt it.

    Capable of failing against an implementation that encrypts on the way out of the API but stores
    plaintext — which is what "we encrypt the secret" often turns out to mean.
    """
    device = MfaDeviceFactory.create()
    plaintext = device.secret

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT secret_ciphertext FROM identity_mfadevice WHERE id = %s", [str(device.pk)]
        )
        stored = cursor.fetchone()[0]

    assert plaintext not in stored
    assert is_encrypted(stored)
    assert MfaDevice.objects.get(pk=device.pk).secret == plaintext


def test_a_dump_of_the_column_does_not_generate_valid_codes() -> None:
    """The finding, stated as a test: the stored value is not a usable TOTP seed.

    Capable of failing because it does the attack. It takes what a dump would yield and tries to
    mint a code with it, rather than merely asserting the string looks different.
    """
    device = MfaDeviceFactory.create(confirmed=True)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT secret_ciphertext FROM identity_mfadevice WHERE id = %s", [str(device.pk)]
        )
        stolen = cursor.fetchone()[0]

    # B017/PT011: "any failure at all" is precisely the assertion. Naming an exception type here
    # would pin the test to pyotp's internals rather than to the property that matters.
    with pytest.raises(Exception):  # noqa: B017, PT011
        pyotp.TOTP(stolen).now()


def test_totp_verification_still_works_end_to_end() -> None:
    """The control still functions — encryption that broke login would be a different bug."""
    from identity.services import verify_totp

    device = MfaDeviceFactory.create(confirmed=True)
    code = pyotp.TOTP(device.secret).now()

    assert verify_totp(device, code) is True


# ------------------------------------------------------------------------------------------------
# The account number
# ------------------------------------------------------------------------------------------------


def test_a_customer_account_gets_a_number() -> None:
    account = AccountFactory.create(account_type=AccountType.ASSET)
    assert numbers.is_valid(account.number)
    assert account.number_last4 == account.number[-7:-3]


def test_bookkeeping_accounts_get_no_number(instrument: Instrument) -> None:
    """Equity, income and instrument accounts are the other side of money, not something held.

    Capable of failing against a ``save`` that numbers everything — which would put the shares
    outstanding contra and every ``Opening balances`` account into any list that renders a number.
    """
    equity = AccountFactory.create(account_type=AccountType.EQUITY)
    income = AccountFactory.create(account_type=AccountType.INCOME)
    position = AccountFactory.create(account_type=AccountType.ASSET, instrument=instrument)

    for account in (equity, income, position):
        assert account.number_ciphertext == ""
        assert account.masked_number == ""


def test_the_number_is_ciphertext_in_the_database() -> None:
    account = AccountFactory.create(account_type=AccountType.ASSET)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT number_ciphertext FROM accounts_account WHERE id = %s", [str(account.pk)]
        )
        stored = cursor.fetchone()[0]

    assert account.number not in stored
    assert is_encrypted(stored)


def test_a_number_is_not_reassigned_on_a_later_save() -> None:
    """An account number that changed under a customer would be worse than not having one."""
    account = AccountFactory.create(account_type=AccountType.ASSET)
    original = account.number
    account.name = "Renamed"
    account.save()
    account.refresh_from_db()
    assert account.number == original


def test_the_list_endpoint_masks_and_the_detail_endpoint_does_not(
    auth_client: APIClient, password_user: User
) -> None:
    """The split that keeps decryption off the common read path.

    Capable of failing in both directions: a list that returned the full number fails the first
    assertion, and a detail view that masked it fails the second.
    """
    account = AccountFactory.create(owner=password_user, account_type=AccountType.ASSET)
    fund_account(account, Decimal("10.00"))

    listed = auth_client.get("/api/v1/accounts/").json()["results"]
    row = next(r for r in listed if r["id"] == str(account.pk))
    assert row["number"] == account.masked_number
    assert account.number not in str(listed)

    detail = auth_client.get(f"/api/v1/accounts/{account.pk}/").json()
    assert detail["number"] == account.number


def test_another_users_account_number_is_not_reachable(auth_client: APIClient) -> None:
    """Owner scoping is unchanged by any of this — the detail view still 404s."""
    stranger = AccountFactory.create(owner=UserFactory.create(), account_type=AccountType.ASSET)
    assert auth_client.get(f"/api/v1/accounts/{stranger.pk}/").status_code == 404


# ------------------------------------------------------------------------------------------------
# The backfills
# ------------------------------------------------------------------------------------------------


def test_the_mfa_backfill_is_a_no_op_once_the_column_is_gone() -> None:
    """Re-running after the migration must not double-wrap.

    Double-wrapping would be unrecoverable: the inner layer decrypts to a base32-looking string that
    is not the secret, so every enrolled user would be locked out with no way back. The plaintext
    column no longer exists in the current registry, which is exactly the state a re-run meets.
    """
    from django.apps import apps as django_apps

    from identity.backfills import encrypt_mfa_secrets

    device = MfaDeviceFactory.create()
    plaintext = device.secret

    assert encrypt_mfa_secrets(django_apps) == 0
    assert MfaDevice.objects.get(pk=device.pk).secret == plaintext


def test_the_account_number_backfill_skips_numbered_accounts() -> None:
    from django.apps import apps as django_apps

    from accounts.backfills import assign_missing_numbers

    account = AccountFactory.create(account_type=AccountType.ASSET)
    original = account.number

    assert assign_missing_numbers(django_apps) == 0
    assert Account.objects.get(pk=account.pk).number == original
