"""Make the audit log append-only in the database itself (ADR-0014).

Two deliberate differences from the ledger's balance trigger, both of which matter:

1. ``BEFORE``, not a ``CONSTRAINT TRIGGER``. Constraint triggers are AFTER-only and deferrable,
   which is right for the zero-sum invariant because it is only true once every line of an entry
   has been written. Here there is no multi-statement invariant to wait for: the write should be
   refused at the statement, not at COMMIT.

2. ``TRUNCATE`` is deliberately **not** blocked. Django's Postgres ``sql_flush`` uses
   ``TRUNCATE ... CASCADE``, which is exactly how ``pytest.mark.django_db(transaction=True)``
   resets between tests — and ``tests/test_concurrency.py`` has three such tests. A
   ``BEFORE TRUNCATE`` trigger would kill them at teardown the moment any audit row existed.

   The honest boundary, stated the way ADR-0010 states its overdraft scope: row-level mutation of
   an audit row is impossible for any client, including raw SQL. Wholesale truncation by the table
   owner is not defended against, and is the mechanism the test harness relies on. Defending
   against a compromised database superuser is a ship-the-logs-off-the-box problem, not a trigger
   problem.
"""

from django.db import migrations

CREATE_FUNCTION = """
CREATE FUNCTION audit_event_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_auditevent is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION = "DROP FUNCTION audit_event_append_only();"

CREATE_TRIGGER = """
CREATE TRIGGER audit_event_append_only
    BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION audit_event_append_only();
"""

DROP_TRIGGER = "DROP TRIGGER audit_event_append_only ON audit_auditevent;"


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_FUNCTION, reverse_sql=DROP_FUNCTION),
        migrations.RunSQL(sql=CREATE_TRIGGER, reverse_sql=DROP_TRIGGER),
    ]
