"""Post a few toy entries by hand for visual feedback (Weeks 2–3).

    uv run python manage.py post_demo_entries

Seeds a demo user with checking, savings, and an equity account; posts two balanced entries
through the posting service and one transfer through the transfer service; then prints each
account's derived balance. Idempotent on the user/accounts (get-or-create) but appends fresh
entries each run — this is a build-time sanity toy, not the Faker seed script (that lands in
`accounts.seed_demo`).
"""

from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from accounts.models import Account, AccountType
from ledger.services import LineSpec, get_balance, post_entry, transfer


class Command(BaseCommand):
    help = "Seed a demo user with two accounts and post a couple of balanced journal entries."

    def handle(self, *args: Any, **options: Any) -> None:
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            username="demo",
            defaults={"email": "demo@example.com"},
        )

        checking, _ = Account.objects.get_or_create(
            owner=user,
            name="Demo Checking",
            defaults={"account_type": AccountType.ASSET},
        )
        savings, _ = Account.objects.get_or_create(
            owner=user,
            name="Demo Savings",
            defaults={"account_type": AccountType.ASSET},
        )
        opening_equity, _ = Account.objects.get_or_create(
            owner=user,
            name="Demo Opening Balances",
            defaults={"account_type": AccountType.EQUITY},
        )

        # Opening deposit: credit checking, debit the opening-balances equity account.
        post_entry(
            description="Opening deposit",
            lines=[
                LineSpec(account=checking, amount=Decimal("500.00")),
                LineSpec(account=opening_equity, amount=Decimal("-500.00")),
            ],
        )
        # A small withdrawal back out.
        post_entry(
            description="ATM withdrawal",
            lines=[
                LineSpec(account=checking, amount=Decimal("-120.50")),
                LineSpec(account=opening_equity, amount=Decimal("120.50")),
            ],
        )

        # Week 3: money between two accounts goes through the transfer service, which locks both
        # rows and checks the balance under that lock.
        entry, created = transfer(
            source=checking,
            destination=savings,
            amount=Decimal("75.25"),
            description="Demo transfer to savings",
        )
        verb = "Posted" if created else "Replayed"
        self.stdout.write(f"{verb} transfer {entry.id}")

        self.stdout.write(self.style.SUCCESS("Posted demo entries. Derived balances:"))
        for account in (checking, savings, opening_equity):
            self.stdout.write(f"  {account.name}: {get_balance(account)}")
