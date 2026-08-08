"""Scheduled identity housekeeping."""

import logging
from typing import Any

from celery import shared_task
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task(name="identity.flush_expired_tokens")
def flush_expired_tokens() -> dict[str, Any]:
    """Delete SimpleJWT's records of tokens that can no longer be presented.

    ``OutstandingToken`` gains a row per login and never loses one, so without this it grows for the
    life of the deployment — harmless at demo volume, which is why it slid from Week 4 to Week 5 to
    here, and unbounded regardless.

    Wraps the management command SimpleJWT already ships rather than reimplementing the query: the
    definition of "expired" belongs to the library that mints the tokens, and a hand-rolled
    ``DELETE`` here would drift from it the first time the library changed its retention rule.
    """
    call_command("flushexpiredtokens")
    logger.info("Flushed expired outstanding tokens.")
    return {"flushed": True}
