"""Add the column, nullable and unconstrained (ADR-0024).

Split from the backfill and the CHECK deliberately: a column added and constrained in one migration
would have to be validated against rows that do not yet carry a value. Three steps — add, fill,
then enforce — is the only order in which the constraint can be validated rather than trusted.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0004_journalline_quantity_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="journalentry",
            name="payload_fingerprint",
            field=models.CharField(blank=True, max_length=80, null=True),
        ),
    ]
