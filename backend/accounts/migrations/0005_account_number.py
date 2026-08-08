"""Give every customer account a number, envelope-encrypted (ADR-0027).

Two columns rather than one: the ciphertext, and the last four digits in plaintext. The split is
what lets a list of accounts render "BK-••••••6789" without decrypting anything, which is the read
that happens constantly — the full number is decrypted only on the detail endpoint.
"""

from django.db import migrations, models

from accounts.backfills import assign_missing_numbers


def forwards(apps, schema_editor):
    assign_missing_numbers(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_remove_account_position_account_is_an_asset_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="number_ciphertext",
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name="account",
            name="number_last4",
            field=models.CharField(blank=True, max_length=4),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
