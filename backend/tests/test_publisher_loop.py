"""The event loop every publish runs on.

``RedisChannelLayer`` keys its connection pool on the running event loop, and ``async_to_sync``
from a thread with no loop builds a new one per call. That combination opened a Redis connection
for every publish and left it in TIME_WAIT: measured at 0.99 connections per publish, which
exhausts the ephemeral port range under a sustained write burst and turns every subsequent publish
into ``Cannot assign requested address``.

These pin the two properties that make the fix a fix. The connection count itself is not asserted
here, because doing that honestly needs a real Redis and a real burst; it is measured in
``deploy/loadtest/RESULTS.md`` instead.
"""

import asyncio
import os
import threading

import pytest

from realtime import events

pytestmark = pytest.mark.django_db


def test_every_publish_shares_one_loop() -> None:
    """The whole point: one loop means the channel layer builds one connection pool."""
    first = events._publisher.get()
    second = events._publisher.get()

    assert first is second
    assert not first.is_closed()


def test_the_loop_is_running_on_its_own_thread() -> None:
    """A loop nobody runs would accept coroutines and never execute them.

    ``run_coroutine_threadsafe`` returns a future against a loop whether or not that loop is being
    driven, so a publisher whose thread had died would not raise; it would block every caller for
    the full send timeout and then log a timeout that says nothing about the cause.
    """
    loop = events._publisher.get()

    assert loop.is_running()
    names = {thread.name for thread in threading.enumerate()}
    assert "realtime-publisher" in names


def test_the_loop_is_rebuilt_after_a_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    """Celery forks its workers, and a thread does not survive ``fork()``.

    The child inherits a loop object whose thread does not exist in the child, so without this
    guard every publish in a forked worker would block for the send timeout and then log a failure.
    Beat and the market tick run in exactly those workers, so the symptom would be that prices stop
    reaching every connected client while the web process keeps working.
    """
    before = events._publisher.get()

    # The real pid is captured first. A lambda calling `os.getpid()` would be calling itself.
    real_pid = os.getpid()
    monkeypatch.setattr(os, "getpid", lambda: real_pid + 1)
    after = events._publisher.get()

    assert after is not before, "the loop survived a simulated fork"
    assert after.is_running()


def test_a_publish_actually_runs_on_that_loop() -> None:
    """Not merely scheduled: the coroutine is executed and its result returned to the caller."""
    loop = events._publisher.get()
    observed: list[asyncio.AbstractEventLoop] = []

    async def probe() -> str:
        observed.append(asyncio.get_running_loop())
        return "ran"

    future = asyncio.run_coroutine_threadsafe(probe(), loop)

    assert future.result(timeout=events.SEND_TIMEOUT_SECONDS) == "ran"
    assert observed == [loop]


def test_a_failing_publish_never_reaches_the_caller() -> None:
    """The rule the module exists to protect: a dropped notification cannot fail a committed write.

    Asserted against a layer that raises, rather than by trusting the ``except`` to be reached.
    """

    class _Broken:
        async def group_send(self, group: str, message: dict[str, object]) -> None:
            raise RuntimeError("redis is gone")

    events._send.__globals__["get_channel_layer"] = lambda: _Broken()
    try:
        events._send("any.group", {"type": events.STREAM_EVENT})
    finally:
        from channels.layers import get_channel_layer

        events._send.__globals__["get_channel_layer"] = get_channel_layer
