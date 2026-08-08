"""Reconcile the ledger against the invariants it claims (ADR-0026).

    uv run python manage.py check_ledger_invariants

Exits non-zero if anything is broken, so it works as a cron guard and a deploy gate as well as a
thing a human runs. Also writes an audit row either way: "we looked, and here is what we found" is
itself a fact worth keeping, and a gap in the sequence of those rows says the check stopped running.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from audit.models import AuditAction
from audit.services import record_audit
from ledger.reconciliation import check_invariants


class Command(BaseCommand):
    help = "Verify every ledger invariant against committed data; exit non-zero on a violation."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print only violations, not the all-clear. For cron.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        violations = check_invariants(connection)

        record_audit(
            action=AuditAction.LEDGER_RECONCILED,
            actor=None,
            target_type="ledger",
            target_id="all",
            context={
                "violations": len(violations),
                # Bounded: a wholesale corruption should not write a megabyte of JSON into the
                # audit log on top of everything else that has gone wrong.
                "sample": [str(v) for v in violations[:20]],
            },
        )

        if not violations:
            if not options["quiet"]:
                self.stdout.write(self.style.SUCCESS("Ledger invariants hold."))
            return

        for violation in violations:
            self.stderr.write(self.style.ERROR(str(violation)))
        raise CommandError(f"{len(violations)} ledger invariant violation(s).")
