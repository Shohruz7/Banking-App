"""The order side of ADR-0024's column. Same three-step shape as ``ledger/0005``."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("trading", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="payload_fingerprint",
            field=models.CharField(blank=True, max_length=80, null=True),
        ),
    ]
