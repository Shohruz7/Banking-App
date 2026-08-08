"""Correlate log lines with the request that produced them (ADR-0028).

``audit.middleware`` has generated a ``request_id`` per request since Week 4 and put it on every
audit row. Until Week 7 it reached no log line at all, because there was no ``LOGGING`` config —
so the three ``logger.exception`` calls in ``statements.tasks`` and ``realtime.events`` landed on
stderr with nothing tying them to the request, the fill or the user that caused them, and every
``logger.info`` was dropped on the floor by the root logger's WARNING default.

This filter is the join. It never rejects a record — a filter that can drop log lines is a filter
that will eventually drop the one you needed.
"""

import logging


class RequestIDFilter(logging.Filter):
    """Attach the ambient ``request_id`` to every record, so the format string can print it.

    Reads the same ``ContextVar`` the audit log reads, which is what makes a log line and an audit
    row joinable on the same key. Work with no request behind it — a Beat task, a management
    command, a shell — gets ``-``, which is information rather than an absence: it says the line
    came from something nobody asked for.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Imported here, not at module scope. ``LOGGING`` is applied while Django is still setting
        # up, before the app registry is populated, and ``audit.context`` reaches
        # ``django.contrib.auth.models`` — importing it at the top makes the whole config fail to
        # load with ``Unable to configure filter``.
        from audit.context import current_context

        record.request_id = current_context().request_id or "-"
        return True
