"""Prove that a price tick published in one container reaches a socket held in another.

**This closes the gap Week 6 wrote down and could not close.** The test suite's channel layer is
in-process (`config/settings/test.py`), so every WebSocket test in `tests/test_ws_events.py` proves
that a publisher and a consumer *inside one Python process* can talk. Nothing proved that a fill
committed in a web worker reaches a socket held by a different process — which is the only
arrangement production ever runs in, and the one thing a Redis channel layer exists to provide.

There is no way to write that as a unit test honestly. Faking it with `multiprocessing` would prove
that two processes on one machine share a Redis, which is not the claim; the claim is about *these*
containers, *this* channel layer configuration and *this* nginx in front of them. So it is a script,
run against the real stack in CI.

What it exercises, end to end:

1. A WebSocket handshake **through nginx** — which is also the only check that `proxy_http_version
   1.1` plus the Upgrade headers are right. Get those wrong and the proxy answers the handshake with
   a 200 and the socket never upgrades.
2. First-frame authentication (ADR-0022): the socket is anonymous until its first message.
3. A subscription to one symbol.
4. A tick published from the **worker** container, and received here.

Run inside the CI stack:
    python deploy/smoke_socket.py --base http://localhost:8080 --username demo --password ...
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

try:
    from websockets.sync.client import connect
except ImportError:  # pragma: no cover - the runner installs it explicitly
    sys.exit("This script needs `websockets`. Install it: pip install websockets")


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        sys.exit(f"POST {url} failed: {error.code} {error.read().decode()[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    tokens = _post(f"{args.base}/api/v1/auth/token/", {
        "username": args.username,
        "password": args.password,
    })
    if "access" not in tokens:
        sys.exit(f"Expected a token pair, got: {json.dumps(tokens)[:300]}")
    print("· authenticated over HTTP", flush=True)

    ws_url = args.base.replace("http://", "ws://").replace("https://", "wss://")
    # An explicit Origin, because `AllowedHostsOriginValidator` wraps the consumer (config/asgi.py)
    # and answers 403 without one. That is not an obstacle to work around — it is the check that
    # stops any page on the internet opening an authenticated socket against this server from a
    # visitor's browser. A browser always sends this header; a bare client library does not, so the
    # script has to behave like the thing it is standing in for.
    with connect(
        f"{ws_url}/ws/v1/stream/",
        open_timeout=20,
        additional_headers={"Origin": args.base},
    ) as socket:
        print("· socket upgraded through nginx", flush=True)

        # ADR-0022: a browser cannot set an Authorization header on a handshake, so the socket
        # authenticates in its first frame rather than with a token in the URL — which would put a
        # bearer credential in every access log between here and the app.
        socket.send(json.dumps({"type": "auth", "token": tokens["access"]}))
        ack = json.loads(socket.recv(timeout=20))
        if ack.get("type") != "auth.ok":
            sys.exit(f"Expected auth.ok, got: {ack}")
        print(f"· authenticated in the first frame as {ack.get('username')}", flush=True)

        socket.send(json.dumps({"type": "subscribe", "symbols": [args.symbol]}))
        print(f"· subscribed to {args.symbol}; waiting for a tick from the worker…", flush=True)

        # The caller triggers `markets.advance_prices` in the worker container after this point.
        # Everything below is the actual assertion: a message crossing a process boundary.
        deadline = args.timeout
        while deadline > 0:
            try:
                frame = json.loads(socket.recv(timeout=min(5.0, deadline)))
            except TimeoutError:
                deadline -= 5.0
                continue

            if frame.get("type") == "price.tick" and frame.get("symbol") == args.symbol:
                print(
                    f"\n✓ tick received: {frame['symbol']} @ {frame['price']} ({frame['at']})",
                    flush=True,
                )
                print(
                    "  Published by the Celery worker, delivered to a socket held by the app "
                    "container, through nginx. The Redis channel layer crosses processes.",
                    flush=True,
                )
                return 0

            # Anything else on the wire is fine — subscription acks, other symbols — but worth
            # showing, because a silent failure here is indistinguishable from a slow one.
            print(f"  (saw {frame.get('type')})", flush=True)

    sys.exit(
        f"No price.tick for {args.symbol} within {args.timeout}s. Either the worker did not "
        "publish, or the channel layer did not carry it across the process boundary — which is "
        "exactly the failure this script exists to catch."
    )


if __name__ == "__main__":
    raise SystemExit(main())
