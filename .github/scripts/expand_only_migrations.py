"""Refuse a migration that narrows the schema while the previous release is still serving.

`deploy.sh` runs `migrate` before it rolls either app replica (ADR-0043), so for the length of a
deploy the *old* code runs against the *new* schema. Anything that removes what the old code still
reads is an outage — a `RemoveField` on a column a running serializer selects is an immediate
ProgrammingError on every request that touches it.

The rule is expand-then-contract: add in one release, stop using it, remove in a later one. This is
the gate that makes that a control rather than a good intention. "By discipline" is what the deploy
runbook said before, and a runbook is not a control.

Argv is the list of changed files; only migrations are examined.
"""

import pathlib
import re
import sys

# Unambiguously destructive: each removes or renames something the previous release may still read.
DESTRUCTIVE = ("RemoveField", "DeleteModel", "RenameField", "RenameModel")
# Raw SQL that drops or narrows. Deliberately narrow — this repo writes triggers with RunSQL, and
# CREATE/REPLACE FUNCTION is routine and safe.
RAW_DESTRUCTIVE = re.compile(r"\b(DROP\s+(TABLE|COLUMN)|ALTER\s+COLUMN\s+\w+\s+SET\s+NOT\s+NULL)\b", re.I)
# The escape hatch, for a contract step that is genuinely safe because nothing reads the column any
# more. Requires a reason on the same line, so it cannot be pasted in without saying why.
EXEMPT = re.compile(r"EXPAND-CONTRACT-EXEMPT:\s*\S+")

problems = []
for name in sys.argv[1:]:
    path = pathlib.Path(name)
    if "migrations" not in path.parts or path.suffix != ".py" or path.name == "__init__.py":
        continue
    if not path.exists():           # deleted in this change; nothing to run
        continue
    source = path.read_text()
    if EXEMPT.search(source):
        print(f"  {name}: exempt, with a stated reason")
        continue
    found = [op for op in DESTRUCTIVE if f"migrations.{op}(" in source or f"{op}(" in source]
    if RAW_DESTRUCTIVE.search(source):
        found.append("destructive RunSQL")
    if found:
        problems.append((name, found))
    elif "AlterField" in source:
        # Not a failure: most AlterFields widen or are cosmetic. Worth a line, because the one that
        # sets NOT NULL on an existing column breaks the old code exactly like a RemoveField does.
        print(f"  {name}: note — contains AlterField; confirm it does not narrow a column")
    else:
        print(f"  {name}: expand-only")

if problems:
    print("\nThese migrations narrow the schema while the previous release is still serving:\n")
    for name, found in problems:
        print(f"  {name}: {', '.join(found)}")
    print(
        "\nSplit it: add in this release, remove in a later one once nothing runs the old code.\n"
        "If it is genuinely safe — nothing reads it any more — say so in the migration:\n"
        '\n    # EXPAND-CONTRACT-EXEMPT: column has been unread since <release>\n'
    )
    sys.exit(1)

print("\nno migration narrows the schema")
