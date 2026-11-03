"""Test ids and the ids of the matches they make.

A test's match is `openbench-<instance>-<test id>`: every match nyayo already holds from OpenBench has that form, and its
pages and exports use it. So a new test must not take an id an old match already has. The first test takes its id above
the highest `N` of those matches, rounded up to the next thousand, and ids count on from there (SQLite's AUTOINCREMENT
never reuses one, even after a test is deleted). That is worked out once, in the migration that creates the tables,
not at runtime; `first_test_id` is the rule and `start_test_ids` applies it.
"""

import re

from django.conf import settings
from django.db import connection


def match_id(test_id, instance=None):
    """The id of the match a test makes: openbench-<instance>-<test id>."""
    return f"openbench-{instance if instance is not None else settings.NYAYO_INSTANCE}-{test_id}"


def first_test_id(match_ids):
    """The id the first test takes, given the ids of the matches that exist.

    Above the highest N in any `openbench-<instance>-<N>`, whatever the instance, rounded up to the next thousand (so
    1234 and 1999 give 2000, and 2000 gives 3000), or 1 when there is none of that form. Any instance, because this
    runs once in a migration: with NYAYO_INSTANCE unset or wrong at that moment, the numbering would restart below the
    old tests and a later correction of the setting would collide.
    """
    pattern = re.compile(r"^openbench-.+-(\d+)$")
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
