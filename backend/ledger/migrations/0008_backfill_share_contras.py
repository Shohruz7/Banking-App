"""Create the shares-outstanding accounts and give every historical fill its contra leg.

Data before enforcement: the trigger in ``0009`` would otherwise be true only of rows written after
the deploy, which is the sort of half-invariant this week exists to remove.

This migration inserts lines into entries that were posted long ago, which is the one deliberate
exception to the append-only wording in ``ledger.models``. It is recorded there and in ADR-0025
rather than left to be discovered: an undocumented exception turns a good docstring into a lie.
"""

from django.db import migrations

from ledger.share_conservation import backfill_contra_lines, ensure_share_contras


def forwards(apps, schema_editor):
    ensure_share_contras(apps)
    backfill_contra_lines(schema_editor.connection)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0007_remove_journalline_journal_line_amount_nonzero_and_more"),
        # The contra accounts are EQUITY rows carrying an instrument, which only became legal in
        # accounts/0004. Cross-app ordering is not guaranteed unless it is named.
        ("accounts", "0004_remove_account_position_account_is_an_asset_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
