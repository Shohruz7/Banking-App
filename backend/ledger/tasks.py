"""Scheduled ledger work — currently just the nightly reconciliation (ADR-0026)."""

import logging

from celery import shared_task
from django.db import connection

from audit.context import audit_context
from audit.models import AuditAction
from audit.services import record_audit
from ledger.reconciliation import check_invariants

logger = logging.getLogger(__name__)


@shared_task(name="ledger.check_invariants")
def check_ledger_invariants() -> dict[str, int]:
    """Scan the whole ledger and record what was found.

    Deliberately does **not** raise on a violation. A Celery task that fails gets retried, and
    retrying a scan that correctly reported corruption reports it again — the useful outputs are the
    audit row and the log line, which happen either way. The management command is the one that
    exits non-zero, because that is where a human or a deploy gate is listening.
    """
    with audit_context(actor=None):
        violations = check_invariants(connection)
        record_audit(
            action=AuditAction.LEDGER_RECONCILED,
            actor=None,
            target_type="ledger",
            target_id="all",
            context={
                "violations": len(violations),
                "sample": [str(v) for v in violations[:20]],
            },
        )

    if violations:
        logger.error(
            "Ledger reconciliation found %d violation(s): %s",
            len(violations),
            "; ".join(str(v) for v in violations[:20]),
        )
    else:
        logger.info("Ledger reconciliation clean.")

    return {"violations": len(violations)}
