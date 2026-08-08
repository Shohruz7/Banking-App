"""What a new customer gets at registration.

Until Week 8 registration created a ``User`` row and stopped. That was invisible while the only
clients were the test suite and ``curl`` — every test builds its own accounts through a factory —
and it became the first thing anyone saw the moment a browser existed: sign up, land on a dashboard
with nothing on it, and be unable to transfer or trade, because ``POST /orders/`` needs a
``cash_account`` and there wasn't one.

So registration opens two accounts and funds the first with an opening deposit. Three details are
deliberate:

**The deposit is a journal entry like any other.** Money cannot appear; it is credited to Checking
and debited from an equity account, so the opening balance is subject to the same zero-sum trigger
as every other posting (ADR-0008). ``fund_account`` in the test factories has always done exactly
this — this is that pattern, promoted to the real code path.

**The equity account belongs to the customer, not to the system.** ADR-0025's shares-outstanding
contra is system-owned because there is one per *instrument* and concurrent first-fills race to
create it. Neither applies here: this account is created inside the transaction that creates its
owner, so nobody else can be creating it at the same time, and a per-user row needs no unique index
to settle a race that cannot happen. It is invisible either way — ``AccountQuerySet.spendable()``
excludes equity.

**Savings opens empty.** The obvious alternative, splitting the deposit across both, makes the
first transfer a customer can make less interesting. Landing money in one account and leaving the
other at zero means "move some to Savings" is a real thing to do on the first visit.
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User

from accounts.models import Account, AccountType
from audit.models import AuditAction
from audit.services import record_audit

from .models import JournalEntry
from .services import LineSpec, post_entry

#: The accounts every new customer opens with. Checking first — it is the one that gets funded and
#: the one every "from" dropdown will default to.
CHECKING_NAME = "Checking"
SAVINGS_NAME = "Savings"

#: The equity account the opening deposit is debited from. Same name the test factories use, which
#: is not a coincidence: they were modelling this before it existed.
OPENING_BALANCES_NAME = "Opening balances"

_ZERO = Decimal("0.0000")


def open_starter_accounts(user: User) -> tuple[Account, Account]:
    """Open Checking and Savings for a brand-new user, funding Checking.

    Returns the pair in the order they are opened. Call inside the transaction that creates the
    user: a customer who exists without accounts is the state this function exists to prevent, and
    a half-applied registration would recreate it.

    With ``ONBOARDING_OPENING_DEPOSIT`` set to zero the accounts are still opened and no entry is
    posted — which is what Week 9's seed script wants, since it funds its own users and would
    otherwise have to unpick a deposit it never asked for.
    """
    checking = Account.objects.create(
        owner=user, name=CHECKING_NAME, account_type=AccountType.ASSET, currency="USD"
    )
    savings = Account.objects.create(
        owner=user, name=SAVINGS_NAME, account_type=AccountType.ASSET, currency="USD"
    )

    deposit: Decimal = settings.ONBOARDING_OPENING_DEPOSIT
    if deposit > _ZERO:
        _post_opening_deposit(user, checking, deposit)

    record_audit(
        action=AuditAction.ACCOUNTS_OPENED,
        actor=user,
        target_type="account",
        target_id=str(checking.pk),
        context={
            "accounts": [str(checking.pk), str(savings.pk)],
            "opening_deposit": deposit,
        },
    )
    return checking, savings


def _post_opening_deposit(user: User, checking: Account, amount: Decimal) -> JournalEntry:
    """Credit Checking, debit the customer's own opening-balances equity account."""
    opening = Account.objects.create(
        owner=user,
        name=OPENING_BALANCES_NAME,
        account_type=AccountType.EQUITY,
        currency="USD",
    )
    return post_entry(
        description="Opening deposit",
        lines=[
            LineSpec(account=opening, amount=-amount),
            LineSpec(account=checking, amount=amount),
        ],
    )
