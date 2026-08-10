"""Generate statements for a month by hand.

The same task Beat runs, reachable without waiting for the 1st — which is how the demo seed
will produce a year of statements in one go, and how a missed month is backfilled after an outage.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from statements.services import parse_period, previous_month
from statements.tasks import generate_monthly_statements


class Command(BaseCommand):
    help = "Generate monthly statements for a period (default: the month that just closed)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--period",
            help="Month to generate, as YYYY-MM. Defaults to the previous calendar month.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        raw: str | None = options.get("period")
        try:
            window = parse_period(raw) if raw else previous_month()
        except ValueError as exc:
            raise CommandError(f"Could not read '{raw}' as a YYYY-MM period.") from exc

        # Called directly rather than through .delay(): a management command that returned before
        # doing anything would be useless for a seed script, and there may be no worker running.
        result = generate_monthly_statements(window.label)
        self.stdout.write(
            self.style.SUCCESS(
                f"{window.label}: {result['cash']} cash, {result['brokerage']} brokerage, "
                f"{result['skipped']} skipped, {result['failed']} failed"
            )
        )
