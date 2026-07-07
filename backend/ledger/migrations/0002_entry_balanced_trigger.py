"""Enforce the zero-sum invariant in the database itself (ADR-0008).

A DEFERRABLE INITIALLY DEFERRED constraint trigger re-sums each entry's lines at COMMIT — after
the header and every line have been inserted in one transaction — and aborts the transaction if
the total is non-zero. This makes an unbalanced ledger impossible even for a buggy code path or a
raw SQL insert, not just for callers who go through ``ledger.services.post_entry``.
"""

from django.db import migrations

CREATE_FUNCTION = """
CREATE FUNCTION assert_entry_balanced() RETURNS trigger AS $$
DECLARE
    target_entry uuid;
    total numeric;
BEGIN
    target_entry := COALESCE(NEW.entry_id, OLD.entry_id);
    SELECT COALESCE(SUM(amount), 0) INTO total
    FROM ledger_journalline
    WHERE entry_id = target_entry;
    IF total <> 0 THEN
        RAISE EXCEPTION 'journal entry % is unbalanced: lines sum to %', target_entry, total;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION = "DROP FUNCTION assert_entry_balanced();"

CREATE_TRIGGER = """
CREATE CONSTRAINT TRIGGER journal_entry_balanced
    AFTER INSERT OR UPDATE OR DELETE ON ledger_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();
"""

DROP_TRIGGER = "DROP TRIGGER journal_entry_balanced ON ledger_journalline;"


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_FUNCTION, reverse_sql=DROP_FUNCTION),
        migrations.RunSQL(sql=CREATE_TRIGGER, reverse_sql=DROP_TRIGGER),
    ]
