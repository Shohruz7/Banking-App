Week 2 — The ledger spine (build it and prove it)
Goal: a working, tested double-entry ledger core. Still foundational, but this is where real code starts, on the most important piece. By Friday you can create accounts, post balanced entries, read correct balances, and watch unbalanced entries get rejected — with tests proving it.
Decisions to lock:

Precision contract — monetary amounts NUMERIC(20,4) (4 dp headroom, present at 2 dp); prices NUMERIC(20,4); share quantities NUMERIC(20,8) if you support fractional shares (decide now — fractional shares make the brokerage demo richer; I'd say yes). Define this once and never store money as anything but Decimal.
Rounding policy — one quantize() helper used everywhere; ROUND_HALF_EVEN (banker's rounding) internally to avoid bias. Pick it explicitly so it's not accidental.
Balance strategy — derive on read (balance = aggregate sum of lines) as the source of truth for v1. A denormalized balance cache is a later optimization, not now — deriving keeps correctness unambiguous.
Enforcement strategy — defense in depth: validate in the posting service (fast, good error messages) and enforce at the database (see below) so the ledger literally cannot go unbalanced.

Build tasks:

Implement the models: Account, JournalEntry (the transaction header), JournalLine (account FK + signed Decimal amount + currency). Add the indexes you'll actually query on (account + created_at).
Write the posting primitive — post_entry(lines) — that opens transaction.atomic(), asserts the lines sum to zero, and creates the entry + lines atomically. No transfers yet (that's Week 3); this is just the core posting function everything else will call.
The database guarantee (this is a genuine flex): add a Postgres deferred constraint trigger via a RunSQL migration that re-sums an entry's lines and raises if the total isn't zero. The key is DEFERRABLE INITIALLY DEFERRED so the check runs at COMMIT, after both the header and all lines are inserted in the same transaction:

sql  CREATE CONSTRAINT TRIGGER journal_entry_balanced
    AFTER INSERT OR UPDATE OR DELETE ON ledger_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();
Now even a buggy code path or a raw SQL insert can't leave the ledger unbalanced. That "the database itself enforces the invariant" line is hard for an interviewer to argue with.

Register the models in Django admin so you get visual feedback while building, and add a small management-command stub that posts a couple of toy entries by hand.

Tests (the heart of the week):

Unbalanced entry is rejected — by both the service and the trigger (test them independently).
Balanced entry posts and balances compute correctly across multiple accounts.
Decimal precision/rounding: no float drift, quantize behaves exactly as specified at the cent boundary.
A select_for_update locking test that demonstrates the intent (you'll flesh out full concurrency tests in Week 3 once transfers exist).

Done when: you can create accounts and post balanced journal entries; balances are correct and derived; unbalanced entries fail at commit via the deferred trigger; money is exact Decimal with a defined rounding policy; and the whole thing is covered by a passing suite in CI. That's the single most important foundation in the project, built and proven before any feature sits on top of it.