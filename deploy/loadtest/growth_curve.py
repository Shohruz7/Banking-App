"""How a derived-balance read scales with an account's history, measured through nginx.

    python deploy/loadtest/growth_curve.py --base http://localhost:8080 --tokens bench_tokens.json

**The question this answers is the one the design is actually about.** Balances are not stored;
``/accounts/`` and ``/portfolio/`` sum an account's signed journal lines on every request
(ADR-0008, ADR-0020). ``tests/test_query_counts.py`` proves the query *count* stays flat as
accounts are added, which rules out an N+1 and says nothing about what one of those queries costs.
``deploy/loadtest/api.js`` measures the endpoints at one data size and says so in its own comment.
Neither shows the curve.

``manage.py bench_derived_balance`` builds the fixture: customers whose Checking carries 100, 1k,
10k and 100k journal lines. This measures what a client waits for at each depth.

Through nginx and by service name on the compose network, matching ``api.js`` exactly, because a
comparison between the two numbers is only meaningful if the path is the same. Sequential rather
than concurrent: this is measuring the shape of one query against data size, and adding queueing
would mix in a second variable.

Stdlib only, like ``smoke_socket.py``, so it runs from the host with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

#: Under the 240/min user budget (ADR-0015) with room to spare. Every sample here is one customer's
#: token, and a run that throttled itself would be measuring the throttle.
PACE_SECONDS = 0.3


def _get(url: str, token: str, host: str) -> tuple[int, float]:
    """Return (status, elapsed_ms) for one authenticated GET."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Host": host},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        error.read()
        status = error.code
    return status, (time.perf_counter() - started) * 1000


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation, so every number printed is one measured request."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://web", help="Origin to measure through.")
    # The connection target and the Host header are two different things. `--base` reaches nginx by
    # compose service name, which keeps Docker Desktop's port forward out of the measurement; that
    # makes the request's Host `web`, and DJANGO_ALLOWED_HOSTS deliberately does not list it. nginx
    # forwards `Host: $host` straight through, so without this every request is a 400. Widening
    # ALLOWED_HOSTS instead would undo the reason it is minimal: a name in it matching an nginx
    # service or upstream is what hid the missing proxy headers for nine weeks.
    parser.add_argument("--host-header", default="localhost", help="Host header Django accepts.")
    parser.add_argument(
        "--tokens",
        default="deploy/loadtest/bench_tokens.json",
        help="JSON from mint_bench_tokens.py: [{depth, username, access}, ...]",
    )
    parser.add_argument("--samples", type=int, default=30, help="Requests per endpoint per depth.")
    parser.add_argument("--warmup", type=int, default=3, help="Discarded requests before each set.")
    arguments = parser.parse_args()

    try:
        with open(arguments.tokens) as handle:
            fleet = json.load(handle)
    except OSError as error:
        print(f"cannot read {arguments.tokens}: {error}", file=sys.stderr)
        print("Run mint_bench_tokens.py through the app container first; `make growth` does it.",
              file=sys.stderr)
        return 2

    endpoints = ["/api/v1/accounts/", "/api/v1/portfolio/"]
    results: list[dict[str, object]] = []

    for entry in sorted(fleet, key=lambda item: item["depth"]):
        for endpoint in endpoints:
            url = f"{arguments.base}{endpoint}"

            # Discarded. The first request against a cold connection pays TCP setup and the first
            # query against a cold shared_buffers pays the read from disk; neither is what a
            # steady-state client experiences, and at the shallow depths they would dominate.
            for _ in range(arguments.warmup):
                _get(url, entry["access"], arguments.host_header)
                time.sleep(PACE_SECONDS)

            samples: list[float] = []
            statuses: set[int] = set()
            for _ in range(arguments.samples):
                status, elapsed = _get(url, entry["access"], arguments.host_header)
                statuses.add(status)
                if status == 200:
                    samples.append(elapsed)
                time.sleep(PACE_SECONDS)

            if statuses != {200}:
                print(f"  !! non-200 responses at depth {entry['depth']}: {sorted(statuses)}",
                      file=sys.stderr)

            results.append(
                {
                    "depth": entry["depth"],
                    "endpoint": endpoint,
                    "n": len(samples),
                    "p50": _percentile(samples, 0.50),
                    "p95": _percentile(samples, 0.95),
                    "mean": statistics.fmean(samples) if samples else float("nan"),
                }
            )
            last = results[-1]
            print(
                f"depth {entry['depth']:>7}  {endpoint:<22} "
                f"p50 {last['p50']:>7.1f}ms  p95 {last['p95']:>7.1f}ms  n={last['n']}"
            )

    print()
    print(f"{'endpoint':<22} {'depth':>8} {'p50 ms':>9} {'p95 ms':>9}")
    for row in results:
        print(
            f"{row['endpoint']:<22} {row['depth']:>8} "
            f"{row['p50']:>9.1f} {row['p95']:>9.1f}"
        )

    print()
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
