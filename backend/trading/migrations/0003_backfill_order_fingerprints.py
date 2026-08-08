"""The order side of the backfill, then its CHECK. Mirrors ``ledger/0006``."""

from django.db import migrations, models

from ledger.fingerprints import backfill_order_fingerprints


def forwards(apps, schema_editor):
    backfill_order_fingerprints(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("trading", "0002_order_payload_fingerprint"),
        ("ledger", "0006_backfill_entry_fingerprints"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(idempotency_key__isnull=True)
                | models.Q(payload_fingerprint__isnull=False),
                name="keyed_order_has_a_fingerprint",
            ),
        ),
    ]
