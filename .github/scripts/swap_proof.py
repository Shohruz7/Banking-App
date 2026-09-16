"""Hammer the API with a connection-reusing session while a replica swap happens.

Used by CI. Writes {"total": N, "failures": [...]} to the path in argv[2] when argv[1] appears.

A `requests.Session` and not `curl` in a loop, because the failure this design is most exposed to
only happens on a *reused* connection: a request written onto a pooled keepalive connection that
the backend is simultaneously closing counts as already-sent, and nginx does not replay an
already-sent POST to the surviving peer. A fresh connection per request would never reach that path
and the check would pass without testing anything.

The interval is not tuning, it is a correctness constraint. One customer's read budget is 240/min
(ADR-0015), so a probe that polls faster than 4 rps starts collecting 429s from its own throttle —
which is the harness failing, not the swap, but arrives looking identical. 0.3s leaves headroom.
"""

import json
import os
import sys
import time

import requests

SENTINEL, OUT, TOKEN = sys.argv[1], sys.argv[2], os.environ["TOKEN"]
URL = os.environ.get("PROBE_URL", "http://localhost:8080/api/v1/accounts/")
INTERVAL = float(os.environ.get("PROBE_INTERVAL", "0.3"))

session = requests.Session()
session.headers["Authorization"] = f"Bearer {TOKEN}"

total, failures = 0, []
while not os.path.exists(SENTINEL):
    total += 1
    try:
        response = session.get(URL, timeout=10)
        if response.status_code != 200:
            failures.append(f"#{total}: HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001 — a connection error is exactly what we are counting
        failures.append(f"#{total}: {type(exc).__name__}: {exc}")
    time.sleep(INTERVAL)

with open(OUT, "w") as handle:
    json.dump({"total": total, "failures": failures[:25]}, handle)
print(f"requests={total} failures={len(failures)}")
