"""Move TOTP secrets from a plaintext column into envelope ciphertext (ADR-0027).

Three steps, and the order is the point: add the new column, move the data, drop the old one. A
``RenameField`` would leave the plaintext where it was under a new name, and a single ``AlterField``
would leave no step at which the values could be transformed.

The old column is dropped in the same migration rather than left behind "just in case". A column
still holding every user's second factor is not a safety net, it is the finding.
"""

from django.db import migrations, models

from identity.backfills import encrypt_mfa_secrets


def forwards(apps, schema_editor):
    encrypt_mfa_secrets(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="mfadevice",
            name="secret_ciphertext",
            field=models.CharField(default="", max_length=512),
            preserve_default=False,
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name="mfadevice", name="secret"),
    ]
