"""Mint one access token per benchmark customer, tagged with that customer's history depth.

Piped into ``manage.py shell`` inside the app container, exactly like ``mint_tokens.py``:

    docker compose ... exec -T app_blue python manage.py shell --no-imports \
        < deploy/loadtest/mint_bench_tokens.py > deploy/loadtest/bench_tokens.json

Separate from ``mint_tokens.py`` rather than a flag on it, because the two select different
populations and mean different things. That one fans out across the demo dataset to measure
throughput under concurrency; this one picks exactly one customer per depth so
``growth_curve.py`` can vary history length with everything else held still.

The depth is read back from the database rather than parsed out of the username, so the label on
every row is the number of lines that are actually there.
"""

import json
import sys

from django.contrib.auth.models import User
from identity.serializers import _issue_pair
from ledger.models import JournalLine

BENCH_EMAIL_DOMAIN = "bench.invalid"

fleet = []
for user in User.objects.filter(email__endswith=f"@{BENCH_EMAIL_DOMAIN}").order_by("id"):
    checking = user.accounts.filter(name="Checking").first()
    if checking is None:
        continue
    fleet.append(
        {
            "username": user.username,
            "depth": JournalLine.objects.filter(account=checking).count(),
            "access": _issue_pair(user)["access"],
        }
    )

print(f"minted {len(fleet)} benchmark tokens", file=sys.stderr)
print(json.dumps(fleet))
