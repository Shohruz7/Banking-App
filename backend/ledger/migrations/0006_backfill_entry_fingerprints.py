"""Fill every pre-ADR-0024 keyed entry's digest, then make the pairing a database rule.

The order of the two operations is the whole point. Adding the CHECK first would refuse to validate
against legacy rows; adding it ``NOT VALID`` would accept them silently and leave the guarantee true
only for rows written after the deploy — which is the kind of half-invariant this week exists to
remove, not add.

The backfill logic itself lives in ``ledger.fingerprints`` rather than here, because
``coverage.omit`` excludes ``*/migrations/*`` and this is the one piece of code in the week that
rewrites history unattended.
"""

from django.db import migrations, models

from ledger.fingerprints import backfill_entry_fingerprints


def forwards(apps, schema_editor):
    backfill_entry_fingerprints(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0005_journalentry_payload_fingerprint"),
        # The entry backfill reads the Order behind every ``order:{pk}`` key, and the order backfill
        # that follows needs its own column. Naming this explicitly is not optional — cross-app
        # migration ordering is not otherwise guaranteed.
        ("trading", "0002_order_payload_fingerprint"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="journalentry",
            constraint=models.CheckConstraint(
                condition=models.Q(idempotency_key__isnull=True)
                | models.Q(payload_fingerprint__isnull=False),
                name="keyed_entry_has_a_fingerprint",
            ),
        ),
    ]
