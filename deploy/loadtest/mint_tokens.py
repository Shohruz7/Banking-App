"""Mint one access token per seeded customer, for the load harness to fan out across.

Piped into ``manage.py shell`` inside the app container:

    docker compose ... exec -T app python manage.py shell --no-imports \
        < deploy/loadtest/mint_tokens.py > deploy/loadtest/tokens.json

**Why this exists rather than logging in over HTTP.** The throttles are the point of the load test
— the run is supposed to prove the stack serves real traffic *inside* its shipped rate limits
(ADR-0015), not with them switched off. But the `login` scope is 10/min keyed on the client address
(ADR-0038), so a harness on one host that authenticates each of its virtual users would spend the
whole run collecting 429s from the one endpoint nobody wanted to measure. Token acquisition is
setup, not workload; it gets to skip the queue.

It skips *only* the queue. ``_issue_pair`` is the same function the login endpoint calls, so every
token here is bound to a real ``AuthSession`` and carries the ``sid`` claim that
``SessionAwareJWTAuthentication`` demands — the auth path under load is the production one.

Emits, per eligible customer:

    {"username": ..., "access": ..., "accounts": ["<uuid>", "<uuid>"]}

Two accounts because the write scenario transfers between a customer's own checking and savings.
Customers with fewer are dropped rather than paired with a stranger: a load test that moves money
between unrelated people would be exercising a path the product does not have.

**Both accounts must be funded**, which is not fussiness. The first version of this took each
customer's first two spendable accounts regardless of balance, and the seed leaves some savings
accounts empty — so the write scenario spent part of its budget posting transfers that were
correctly rejected for insufficient funds. Those 400s are the ledger working, but they are fast,
and a latency figure quietly averaged over rejections is measuring the wrong path.
"""

import json
import sys
from decimal import Decimal

from accounts.models import Account
from django.contrib.auth.models import User
from identity.serializers import _issue_pair

SEED_EMAIL_DOMAIN = "demo.invalid"

customers = (
    User.objects.filter(email__endswith=f"@{SEED_EMAIL_DOMAIN}", is_active=True)
    .order_by("id")
    .prefetch_related("accounts")
)

#: Comfortably more than the run can move. The write scenario posts 1.00 at a time and alternates
#: direction, so the net drift over a run is ~zero — this only has to cover the transient.
MIN_BALANCE = Decimal("100")

fleet = []
for user in customers:
    spendable = [
        account
        for account in Account.objects.filter(owner=user).spendable().with_balance().order_by("id")
        if account.balance >= MIN_BALANCE
    ]
    if len(spendable) < 2:
        continue
    fleet.append(
        {
            "username": user.username,
            "access": _issue_pair(user)["access"],
            "accounts": [str(account.id) for account in spendable[:2]],
        }
    )

# stderr, so the JSON on stdout stays machine-readable.
print(f"minted {len(fleet)} tokens", file=sys.stderr)
print(json.dumps(fleet))
