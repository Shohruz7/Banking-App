"""Enforce share conservation in the database itself (ADR-0025).

The sibling of ``0002``'s zero-sum trigger, and deliberately the same shape: a DEFERRABLE INITIALLY
DEFERRED constraint trigger that re-checks the whole entry at COMMIT, so an entry is judged once it
is complete rather than mid-construction.

**Why it groups.** A flat ``SUM(quantity) = 0`` over the entry would pass this:

    AAPL position  +19500.0000  qty +100
    PENY position  -19500.0000  qty -100

— amount-balanced, quantity-net-zero, and a hundred shares of a penny stock turned into Apple. The
unit is what makes conservation mean anything, so the sum groups by the instrument behind each
account. This is the multi-unit invariant ADR-0016 declined to build, with the unit resolved by join
rather than stored on the line.

**Why the NULL group stays in.** Cash lines group under ``instrument_id IS NULL``. Their
quantities are all NULL, so the group sums to NULL and passes — unless someone puts a quantity on a
cash line, which ``post_entry`` forbids and nothing in the database previously caught. Leaving the
group in closes that too.

The final ``RunPython`` is not decoration. ``CREATE CONSTRAINT TRIGGER`` has no ``NOT VALID`` and
runs no validation scan, so without it this migration would apply silently over a failed backfill,
and the first trade to touch a bad entry would be the discovery mechanism.
"""

from django.db import migrations

from ledger.share_conservation import find_unconserved_entries

CREATE_FUNCTION = """
CREATE FUNCTION assert_entry_conserves_shares() RETURNS trigger AS $$
DECLARE
    target_entry uuid;
    offender record;
BEGIN
    target_entry := COALESCE(NEW.entry_id, OLD.entry_id);
    SELECT a.instrument_id AS instrument_id, SUM(l.quantity) AS net
      INTO offender
      FROM ledger_journalline l
      JOIN accounts_account a ON a.id = l.account_id
     WHERE l.entry_id = target_entry
     GROUP BY a.instrument_id
    HAVING COALESCE(SUM(l.quantity), 0) <> 0
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION
            'journal entry % does not conserve shares of instrument %: net %',
            target_entry, offender.instrument_id, offender.net;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION = "DROP FUNCTION assert_entry_conserves_shares();"

CREATE_TRIGGER = """
CREATE CONSTRAINT TRIGGER journal_entry_conserves_shares
    AFTER INSERT OR UPDATE OR DELETE ON ledger_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_conserves_shares();
"""

DROP_TRIGGER = "DROP TRIGGER journal_entry_conserves_shares ON ledger_journalline;"


def verify(apps, schema_editor):
    """Refuse to finish if any entry still fails the invariant the trigger is about to enforce."""
    offenders = find_unconserved_entries(schema_editor.connection)
    if offenders:
        listed = ", ".join(f"entry {e} instrument {i} net {n}" for e, i, n in offenders[:10])
        raise RuntimeError(
            f"{len(offenders)} journal entries do not conserve shares; "
            f"the 0008 backfill did not complete. First offenders: {listed}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0008_backfill_share_contras"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_FUNCTION, reverse_sql=DROP_FUNCTION),
        migrations.RunSQL(sql=CREATE_TRIGGER, reverse_sql=DROP_TRIGGER),
        migrations.RunPython(verify, migrations.RunPython.noop),
    ]
