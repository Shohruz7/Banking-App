"""Idempotency keys are bound to the request that first used them (ADR-0024).

Two properties, and they are not the same property:

* a key reused for a *different* request is a conflict, not a replay — the ADR-0010 v1 limitation;
* a key reused by a *different user* discloses nothing — a live bug before this week, because
  ``idempotency_key`` is unique across the whole table and neither lookup was owner-scoped.

Several tests below are written so they fail against the pre-ADR-0024 code, which is the only way to
show the second property was ever missing.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from rest_framework.test import APIClient

from accounts.models import Account, AccountType
from ledger.exceptions import IdempotencyKeyConflictError, InvalidEntryError
from ledger.fingerprints import fingerprint, transfer_fingerprint
from ledger.models import JournalEntry
from ledger.services import LineSpec, get_balance, post_entry, transfer
from markets.models import Instrument
from trading.exceptions import OrderKeyConflictError
from trading.models import Order, OrderSide, OrderType
from trading.services import place_order

from .factories import AccountFactory, UserFactory, fund_account

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------------------------------------
# Canonicalization
# ------------------------------------------------------------------------------------------------


def test_the_same_transfer_expressed_differently_is_still_a_replay() -> None:
    """``Decimal("25")`` and ``Decimal("25.0000")`` are one request, not two.

    Capable of failing because a digest built with ``str(payload)`` — the obvious implementation —
    hashes "25" and "25.0000" differently and turns the retry into a 409. The assertion is on
    ``created`` and on the entry count, so "always replay" cannot pass it either: the first call
    must still have posted.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    first, created_first = transfer(
        source=source, destination=destination, amount=Decimal("25"), idempotency_key="k"
    )
    second, created_second = transfer(
        source=source, destination=destination, amount=Decimal("25.0000"), idempotency_key="k"
    )

    assert created_first is True
    assert created_second is False
    assert second.pk == first.pk
    assert JournalEntry.objects.filter(idempotency_key="k").count() == 1
    # The money moved once. Without this the test would pass against a service that posted twice
    # and merely returned the first entry.
    assert get_balance(source) == Decimal("75.0000")


def test_a_digest_is_independent_of_key_order() -> None:
    """The canonical form depends on the request, not on how the dict was built."""
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_a_digest_carries_its_version() -> None:
    """A later payload change bumps the prefix instead of silently invalidating stored rows."""
    digest = transfer_fingerprint(
        source_id=AccountFactory.create().pk,
        destination_id=AccountFactory.create().pk,
        amount=Decimal("1.00"),
    )
    assert digest.startswith("v1:")


# ------------------------------------------------------------------------------------------------
# Same user, different request
# ------------------------------------------------------------------------------------------------


def test_a_key_reused_with_a_different_amount_conflicts() -> None:
    """The ADR-0010 limitation, closed: same key, different amount, no longer a silent replay."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))
    transfer(source=source, destination=destination, amount=Decimal("10.00"), idempotency_key="k")

    with pytest.raises(IdempotencyKeyConflictError):
        transfer(
            source=source, destination=destination, amount=Decimal("99.00"), idempotency_key="k"
        )

    # Nothing was posted by the conflicting call, and nothing moved.
    assert JournalEntry.objects.filter(idempotency_key="k").count() == 1
    assert get_balance(source) == Decimal("90.0000")


def test_a_key_reused_for_a_different_destination_conflicts() -> None:
    """Same amount, different counterparty — plainly a different movement."""
    source = AccountFactory.create()
    first_destination = AccountFactory.create()
    second_destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))
    transfer(
        source=source, destination=first_destination, amount=Decimal("10.00"), idempotency_key="k"
    )

    with pytest.raises(IdempotencyKeyConflictError):
        transfer(
            source=source,
            destination=second_destination,
            amount=Decimal("10.00"),
            idempotency_key="k",
        )


def test_a_retry_with_a_different_description_is_still_a_replay() -> None:
    """``description`` is deliberately outside the digest.

    Capable of failing against the over-strict implementation that hashes the whole request body:
    a client retrying with a tweaked label has not asked for different money to move, and 409-ing
    it would be hostile. The assertion also pins *which* description survives — the first one.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    first, _ = transfer(
        source=source,
        destination=destination,
        amount=Decimal("10.00"),
        description="Rent",
        idempotency_key="k",
    )
    second, created = transfer(
        source=source,
        destination=destination,
        amount=Decimal("10.00"),
        description="Rent (June)",
        idempotency_key="k",
    )

    assert created is False
    assert second.pk == first.pk
    assert second.description == "Rent"


def test_the_conflict_check_survives_the_integrity_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race handler compares digests too — the site most easily left out.

    That branch is unreachable single-threaded, so it is forced the way
    ``test_duplicate_key_race_recovers_the_original_entry`` forces it: both pre-INSERT lookups miss,
    the INSERT collides, and recovery runs. Capable of failing because an implementation that
    compares fingerprints only in the two ordinary lookups reaches the handler, refetches by key
    alone, and hands back an entry for a request the caller never made.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("500.00"))
    transfer(source=source, destination=destination, amount=Decimal("10.00"), idempotency_key="dup")

    from ledger import services

    real_lookup = services._replayed_entry
    misses = {"count": 0}

    def miss_twice(key: str, digest: str | None) -> JournalEntry | None:
        misses["count"] += 1
        return None if misses["count"] <= 2 else real_lookup(key, digest)

    monkeypatch.setattr(services, "_replayed_entry", miss_twice)

    with pytest.raises(IdempotencyKeyConflictError):
        transfer(
            source=source, destination=destination, amount=Decimal("77.00"), idempotency_key="dup"
        )


def test_a_keyed_entry_cannot_lose_its_fingerprint() -> None:
    """The "legacy row" state is unrepresentable, not merely handled.

    The tolerant comparison — ``if stored and stored != new`` — would hand a digest-less entry back
    for any request at all, so the plan was a service-level guard against it. The CHECK turned
    out to be the stronger answer: a keyed entry with a NULL digest cannot exist at all.

    Capable of failing because it drives the state directly with ``update()``, bypassing every
    service. Only ``audit_auditevent`` is protected from UPDATE, so this write reaches the database
    and nothing but the constraint can refuse it.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))
    entry, _ = transfer(
        source=source, destination=destination, amount=Decimal("10.00"), idempotency_key="legacy"
    )

    with pytest.raises(IntegrityError, match="keyed_entry_has_a_fingerprint"):
        JournalEntry.objects.filter(pk=entry.pk).update(payload_fingerprint=None)


def test_post_entry_refuses_a_key_without_a_fingerprint() -> None:
    """The service says so before the database has to, with a domain error rather than a 500.

    Capable of failing because the assertion is on ``InvalidEntryError``: with the guard removed the
    CHECK still refuses the row, but it arrives as ``IntegrityError`` from a frame nowhere near the
    caller's mistake. The test distinguishes which layer caught it.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()

    with pytest.raises(InvalidEntryError):
        post_entry(
            description="keyed but undigested",
            lines=[
                LineSpec(account=source, amount=Decimal("-10.00")),
                LineSpec(account=destination, amount=Decimal("10.00")),
            ],
            idempotency_key="no-digest",
        )

    assert JournalEntry.objects.filter(idempotency_key="no-digest").count() == 0


# ------------------------------------------------------------------------------------------------
# Different user — the disclosure
# ------------------------------------------------------------------------------------------------


def test_another_users_key_does_not_return_their_transfer() -> None:
    """**Fails against the pre-ADR-0024 service.**

    ``idempotency_key`` is unique across the whole table and ``_entry_for_key`` was not scoped by
    owner, so the second user was handed the first user's entry — and ``JournalEntrySerializer``
    puts every line's ``account_id`` and ``amount`` in the response body.

    Capable of failing because the assertion is not merely "an error happened": it names the entry
    that must not come back. A service that returned the stranger's entry satisfies no line here.
    """
    victim = UserFactory.create()
    victim_source = AccountFactory.create(owner=victim)
    victim_destination = AccountFactory.create(owner=victim)
    fund_account(victim_source, Decimal("500.00"))
    victims_entry, _ = transfer(
        source=victim_source,
        destination=victim_destination,
        amount=Decimal("321.00"),
        description="Victim's rent",
        idempotency_key="shared-key",
    )

    attacker = UserFactory.create()
    attacker_source = AccountFactory.create(owner=attacker)
    attacker_destination = AccountFactory.create(owner=attacker)
    fund_account(attacker_source, Decimal("500.00"))

    with pytest.raises(IdempotencyKeyConflictError):
        transfer(
            source=attacker_source,
            destination=attacker_destination,
            amount=Decimal("1.00"),
            idempotency_key="shared-key",
        )

    # The victim's posting is untouched and still the only holder of that key.
    assert JournalEntry.objects.filter(idempotency_key="shared-key").count() == 1
    assert JournalEntry.objects.get(idempotency_key="shared-key").pk == victims_entry.pk


def test_the_conflict_response_discloses_nothing_about_the_original(
    auth_client: APIClient, password_user: User, funded_cash_account: Account
) -> None:
    """The 409 body must not echo the request the key was first bound to.

    Capable of failing against the obvious ``raise IdempotencyKeyConflict(str(exc))``, which would
    put the stranger's amount and account ids into the detail string and reintroduce the very
    disclosure the fingerprint closes.
    """
    victim = UserFactory.create()
    victim_source = AccountFactory.create(owner=victim)
    victim_destination = AccountFactory.create(owner=victim)
    fund_account(victim_source, Decimal("500.00"))
    victims_entry, _ = transfer(
        source=victim_source,
        destination=victim_destination,
        amount=Decimal("321.00"),
        description="Victim's rent",
        idempotency_key="guessable",
    )

    mine = AccountFactory.create(owner=password_user, account_type=AccountType.ASSET)
    response = auth_client.post(
        "/api/v1/transfers/",
        {
            "source_account": str(funded_cash_account.pk),
            "destination_account": str(mine.pk),
            "amount": "1.00",
            "idempotency_key": "guessable",
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_conflict"

    body = response.content.decode()
    for secret in (
        str(victims_entry.pk),
        str(victim_source.pk),
        str(victim_destination.pk),
        "321",
        "Victim's rent",
    ):
        assert secret not in body


# ------------------------------------------------------------------------------------------------
# Orders
# ------------------------------------------------------------------------------------------------


def test_an_order_key_belonging_to_another_user_is_not_replayed(instrument: Instrument) -> None:
    """**Fails against the pre-ADR-0024 service.**

    ``Order.objects.filter(idempotency_key=…)`` carried no ``user=`` filter, so the foreign order —
    symbol, side, quantity, price — was returned to whoever guessed the key.
    """
    victim = UserFactory.create()
    victim_cash = AccountFactory.create(owner=victim, account_type=AccountType.ASSET)
    fund_account(victim_cash, Decimal("10000.00"))
    victims_order = place_order(
        user=victim,
        instrument=instrument,
        cash_account=victim_cash,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("3"),
        limit_price=Decimal("1.00"),
        idempotency_key="shared-order-key",
    )

    attacker = UserFactory.create()
    attacker_cash = AccountFactory.create(owner=attacker, account_type=AccountType.ASSET)
    fund_account(attacker_cash, Decimal("10000.00"))

    with pytest.raises(OrderKeyConflictError):
        place_order(
            user=attacker,
            instrument=instrument,
            cash_account=attacker_cash,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("1"),
            limit_price=Decimal("1.00"),
            idempotency_key="shared-order-key",
        )

    assert Order.objects.filter(idempotency_key="shared-order-key").count() == 1
    assert Order.objects.get(idempotency_key="shared-order-key").pk == victims_order.pk


def test_an_order_key_reused_for_a_different_quantity_conflicts(instrument: Instrument) -> None:
    """The same client, its own key, a different order."""
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user, account_type=AccountType.ASSET)
    fund_account(cash, Decimal("10000.00"))
    place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("3"),
        limit_price=Decimal("1.00"),
        idempotency_key="mine",
    )

    with pytest.raises(OrderKeyConflictError):
        place_order(
            user=user,
            instrument=instrument,
            cash_account=cash,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("9"),
            limit_price=Decimal("1.00"),
            idempotency_key="mine",
        )

    assert Order.objects.filter(idempotency_key="mine").count() == 1


def test_an_identical_order_request_still_replays(instrument: Instrument) -> None:
    """The conflict check must not break the replay it is built on top of."""
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user, account_type=AccountType.ASSET)
    fund_account(cash, Decimal("10000.00"))

    first = place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("3"),
        limit_price=Decimal("1.00"),
        idempotency_key="same",
    )
    second = place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("3.00000000"),
        limit_price=Decimal("1.0000"),
        idempotency_key="same",
    )

    assert second.pk == first.pk
    assert Order.objects.filter(idempotency_key="same").count() == 1
