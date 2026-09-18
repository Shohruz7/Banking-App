"""The derived-balance benchmark command.

Measurement code, but it writes to the ledger through the real services and it mutates
``settings.CHANNEL_LAYERS``, so both of those deserve the same scrutiny as anything else that does.
The depths here are tiny: these assert the mechanics, not the curve. The curve is a laptop
measurement recorded in ``deploy/loadtest/RESULTS.md``, and is not something a test can assert
without pinning a number to a machine.
"""

from io import StringIO

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command

from accounts.models import Account
from ledger.management.commands.bench_derived_balance import BENCH_EMAIL_DOMAIN
from ledger.models import JournalLine

pytestmark = pytest.mark.django_db


def _run(**kwargs: object) -> str:
    out = StringIO()
    call_command("bench_derived_balance", stdout=out, **kwargs)
    return out.getvalue()


def test_it_seeds_an_account_to_the_requested_depth() -> None:
    _run(depths=[12], write_samples=0, progress_every=0)

    user = User.objects.get(username="bench-12")
    assert user.email.endswith(f"@{BENCH_EMAIL_DOMAIN}")
    checking = Account.objects.get(owner=user, name="Checking")
    assert JournalLine.objects.filter(account=checking).count() == 12


def test_the_lines_went_through_the_real_ledger() -> None:
    """Not a fixture: the invariants hold, because every line was posted by ``transfer``.

    This is the whole reason the command does not bulk-insert. A benchmark whose data could not
    have been produced by the application is measuring a table, not a ledger.
    """
    _run(depths=[10], write_samples=0, progress_every=0)

    call_command("check_ledger_invariants", verbosity=0)


def test_a_second_run_does_not_re_seed() -> None:
    _run(depths=[8], write_samples=0, progress_every=0)
    checking = Account.objects.get(owner__username="bench-8", name="Checking")
    assert JournalLine.objects.filter(account=checking).count() == 8

    output = _run(depths=[8], write_samples=0, progress_every=0)

    assert "0 to post" in output
    assert JournalLine.objects.filter(account=checking).count() == 8


def test_the_write_burst_posts_and_is_counted() -> None:
    _run(depths=[6], write_samples=4, progress_every=0)

    checking = Account.objects.get(owner__username="bench-6", name="Checking")
    # Six seeded plus four timed, all real postings.
    assert JournalLine.objects.filter(account=checking).count() == 10
    call_command("check_ledger_invariants", verbosity=0)


def test_the_channel_layer_is_restored_afterwards() -> None:
    """The failure this guards is not hypothetical; ``seed_demo`` documents it having happened.

    The write burst drops ``CHANNEL_LAYERS`` so it prices the overdraft check rather than a Redis
    failure. Under pytest the command shares a process with every test that follows, so leaving the
    setting flattened would break every later WebSocket test on a layer that is ``None``, far from
    the code that caused it.
    """
    before = settings.CHANNEL_LAYERS

    _run(depths=[4], write_samples=2, progress_every=0)

    assert before == settings.CHANNEL_LAYERS


def test_the_reported_aggregate_timing_is_present() -> None:
    output = _run(depths=[5], write_samples=0, progress_every=0)

    assert "Server-side balance aggregate" in output
    assert "Write cost at depth" in output
