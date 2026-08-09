"""The demo seed, and specifically the part of it most able to break silently.

Following the precedent of ``test_seed_instruments.py``: a management command that only ever runs by
hand is a command whose bugs are found by a person noticing a chart looks wrong.

**The backdating is what these tests are really for.** It is the only place in the project that
rewrites a timestamp after the fact, and every failure mode is quiet: a line whose ``created_at``
drifts from its entry disappears from one statement and turns up in no other; an order whose
``resolved_at`` stays at "now" empties every brokerage statement in the dataset; a run that forgot
to sort chronologically would still produce a ledger that balances, and a history that says a
customer spent money they did not yet have.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import F
from django.utils import timezone

from accounts.management.commands.seed_demo import DEMO_USERNAME, SEED_EMAIL_DOMAIN
from audit.models import AuditEvent
from ledger.models import JournalEntry, JournalLine
from markets.models import Instrument
from trading.models import Order

pytestmark = pytest.mark.django_db


@pytest.fixture
def instruments() -> None:
    """A market to trade in. Three symbols is enough; 55 would only slow the test down."""
    for symbol, price in (("AAA", "100.0000"), ("BBB", "50.0000"), ("CCC", "20.0000")):
        Instrument.objects.create(
            symbol=symbol,
            name=f"{symbol} Corp",
            sector="Technology",
            initial_price=Decimal(price),
            last_price=Decimal(price),
            drift=Decimal("0.05"),
            volatility=Decimal("0.20"),
        )


def test_the_seed_produces_a_ledger_legal_dataset(instruments: None) -> None:
    """The claim the whole design rests on: this data could have come from the application.

    It did, in fact — every dollar moved through `transfer` and `place_order` (ADR-0037) — and
    `check_ledger_invariants` exits non-zero if any entry fails to balance or any instrument's
    shares fail to net to zero. Running it here is what turns that design decision into a checked
    property rather than a claim in a docstring.
    """
    call_command("seed_demo", users=4, days=90, seed=1, verbosity=0)

    # Exits non-zero (SystemExit) on a violation, so reaching the next line is the assertion.
    call_command("check_ledger_invariants", verbosity=0)

    assert User.objects.filter(email__endswith=f"@{SEED_EMAIL_DOMAIN}").count() == 4
    assert JournalEntry.objects.exists()
    assert Order.objects.exists()


def test_every_line_carries_its_entrys_timestamp(instruments: None) -> None:
    """A line that drifts from its entry is money missing from one statement and present in none.

    `statements.services._period_lines` and `Account.with_balance(as_of=...)` both filter on the
    *line*, not the entry, so this is not a tidiness property.
    """
    call_command("seed_demo", users=4, days=90, seed=1, verbosity=0)

    assert JournalLine.objects.exclude(created_at=F("entry__created_at")).count() == 0


def test_history_spans_the_requested_window_and_does_not_reach_the_future(
    instruments: None,
) -> None:
    """Backdating actually happened, and stopped where it should.

    Without the rewrite every row would be stamped within a second of the run — the failure this
    catches is not "the dates are slightly off" but "the dates were never changed at all", which is
    what a silently swallowed `bulk_update` looks like.
    """
    days = 90
    before = timezone.now()
    call_command("seed_demo", users=4, days=days, seed=1, verbosity=0)

    oldest = JournalEntry.objects.order_by("created_at").first()
    newest = JournalEntry.objects.order_by("-created_at").first()
    assert oldest is not None and newest is not None

    span_days = (newest.created_at - oldest.created_at).days
    # Generous bounds on purpose: the events are randomly placed, so asserting an exact span would
    # be asserting the RNG. What matters is that this is months rather than seconds.
    assert span_days > days // 2, f"history spans only {span_days} days"
    assert newest.created_at <= timezone.now()
    # The opening deposits are stamped before the window, so nothing precedes a customer's money.
    assert oldest.created_at < before


def test_orders_carry_a_resolution_time_in_the_past(instruments: None) -> None:
    """`build_brokerage_statement` filters fills by `resolved_at`.

    Leaving it at "now" while backdating `created_at` produces a dataset where every brokerage
    statement is empty and nothing anywhere reports an error.
    """
    call_command("seed_demo", users=4, days=90, seed=1, verbosity=0)

    resolved = Order.objects.exclude(resolved_at=None)
    assert resolved.exists(), "no orders filled, so this proves nothing"

    # Every resolution sits at its order's creation: a market order fills inside the request that
    # placed it, so the two instants are genuinely the same one.
    assert resolved.exclude(resolved_at=F("created_at")).count() == 0


def test_the_audit_log_is_not_backdated(instruments: None) -> None:
    """Deliberate, and the opposite of every other timestamp here.

    The log records when the *system* did something, and the system really did write these rows
    today. Backdating an append-only log so that it looks like history is precisely the thing an
    append-only log exists to make impossible — so this asserts the absence of a feature.
    """
    before = timezone.now()
    call_command("seed_demo", users=4, days=90, seed=1, verbosity=0)

    assert AuditEvent.objects.exists()
    assert AuditEvent.objects.filter(created_at__lt=before).count() == 0


def test_a_second_run_refuses_rather_than_merging(instruments: None) -> None:
    """And the refusal explains itself, because the reason is not obvious.

    Seeded customers cannot be deleted once they have acted: `AuditEvent` is append-only by database
    trigger, and `AuditEvent.actor` is `PROTECT`. So there is no `--reset` to offer — the honest
    answer is an empty database, and the error says so.
    """
    call_command("seed_demo", users=3, days=30, seed=1, verbosity=0)

    with pytest.raises(CommandError) as caught:
        call_command("seed_demo", users=3, days=30, seed=1, verbosity=0)

    assert "append-only" in str(caught.value)
    assert "down -v" in str(caught.value)


def test_the_demo_login_exists_and_can_enrol_in_mfa(instruments: None) -> None:
    """The walkthrough signs in as this user and enrols a device on camera.

    Seeding an MFA device would make that step impossible to show, so its absence is a feature.
    """
    call_command("seed_demo", users=4, days=90, seed=1, verbosity=0)

    demo = User.objects.get(username=DEMO_USERNAME)
    assert demo.email == f"{DEMO_USERNAME}@{SEED_EMAIL_DOMAIN}"
    assert not demo.mfa_devices.exists()


def test_it_refuses_to_run_against_an_empty_market() -> None:
    """No instruments means no trades, and a dataset that silently omits the brokerage half."""
    with pytest.raises(CommandError, match="seed_instruments"):
        call_command("seed_demo", users=2, days=30, seed=1, verbosity=0)
