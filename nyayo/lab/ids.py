"""Test ids and the ids of the matches they make.

A test's match is `<mode>-<test id>`: `sprt-1000`, `games-1001`, `tune-1002` (an SPSA), `datagen-1003`. The number is the
test's id, one sequence for all modes, so it names exactly one test and the mode in front is only for the reader and
never changes (a test's mode is fixed when it is made). The matches nyayo took from OpenBench before it ran tests itself
were `openbench-<instance>-<N>`; `manage.py rename_matches` gave them the same form and `OLD_FORM` still recognises
the old one, for the redirects.

The first test took its id above the highest `N` of those old matches, rounded up to the next thousand, and ids count on
from there (SQLite's AUTOINCREMENT never reuses one, even after a test is deleted). That was worked out once, in the
migration that creates the tables, not at runtime; `first_test_id` is the rule and `start_test_ids` applies it.
"""

import re

from django.db import connection

# a test's mode, and what its match id starts with
PREFIXES = {'SPRT': 'sprt', 'GAMES': 'games', 'SPSA': 'tune', 'DATAGEN': 'datagen'}
# the other way, for a match id read back: a prefix is one mode
MODES = {prefix: mode for mode, prefix in PREFIXES.items()}

# the match ids of nyayo's own tests; an outside producer may not use them (matches.views.reserved)
TEST_FORM = re.compile(r'^(?P<prefix>%s)-(?P<number>\d+)$' % '|'.join(MODES))
# the ids the matches from OpenBench's server had before, and now redirect from
OLD_FORM = re.compile(r'^openbench-(?P<instance>.+)-(?P<number>\d+)$')


def match_id(test_id, mode):
    """The id of the match a test makes: <mode prefix>-<test id>. `mode` is the test's test_mode, a Test's column."""
    return f"{PREFIXES[mode]}-{test_id}"


def first_test_id(match_ids):
    """The id the first test takes, given the ids of the matches that exist.

    Above the highest N in any `openbench-<instance>-<N>`, whatever the instance, rounded up to the next thousand (so
    1234 and 1999 give 2000, and 2000 gives 3000), or 1 when there is none of that form. The ids of tests' own
    matches (`sprt-N` ...) count too, so a database that was renamed first doesn't restart below them. Any instance,
    because this runs once in a migration.
    """
    pattern = re.compile(r"^(?:openbench-.+|sprt|games|tune|datagen)-(\d+)$")
    numbers = [int(m.group(1)) for m in map(pattern.match, match_ids) if m]
    if not numbers:
        return 1
    return (max(numbers) // 1000 + 1) * 1000


def start_test_ids(first, conn=None, table="lab_test"):
    """Make the next row of `table` take id `first`: a no-op for 1, and for a table that already has rows past it."""
    if first <= 1:
        return
    conn = conn or connection
    if conn.vendor != "sqlite":
        raise NotImplementedError("nyayo runs on SQLite")
    with conn.cursor() as cursor:
        cursor.execute("SELECT seq FROM sqlite_sequence WHERE name = %s", [table])
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (%s, %s)", [table, first - 1])
        elif row[0] < first - 1:
            cursor.execute("UPDATE sqlite_sequence SET seq = %s WHERE name = %s", [first - 1, table])
