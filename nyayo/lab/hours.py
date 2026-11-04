"""The hourly rollup behind the 48-hour graph on a box's page: one `MachineHour` row per box and hour with the games it
reported, the seconds it was heard from and the round trips measured.

It is written by the calls a worker makes anyway (`note`: one small upsert of the hour's row, a single statement), never by the page, and a failure here never fails a worker's request. A box is its worker, named by its lowercase
name (`machine <id>` when it has none): registration keeps one row for a worker across restarts, so they go on in the same row. Rows are kept `KEEP_DAYS` and pruned by nyayo's worker.

Seconds heard are the time since the session was last heard, counted only when that is under `GAP` (so a worker that
was away does not get credit for the time it was gone), and put in the hour of the call.
"""

import datetime
import logging

from django.db import connection
from django.utils import timezone

from nyayo.lab.models import MachineHour

log = logging.getLogger(__name__)

KEEP_DAYS = 30
GAP = 300          # seconds: the longest silence still counted as the worker being there (machines.ACTIVE)
HOUR = datetime.timedelta(hours=1)


def box_of(machine):
    """The key of a worker: its lowercase name, or `machine <id>` for one with none (a row for each worker, across restarts)."""
    return (machine.name or 'machine %d' % machine.id).lower()[:150]


def start_of(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def note(machine, games=0, now=None):
    """Add this call to its hour: the games it reported, the seconds since the box was last heard, and the response time
    just measured (`rtt.note` ran before). Call it before the machine is saved, while `machine.updated` is still the last
    time. Never raises."""
    try:
        now = now or timezone.now()
        heard = (now - machine.updated).total_seconds() if machine.updated else 0
        heard = int(heard) if 0 < heard < GAP else 0
        measured = machine.rtt_ms if getattr(machine, 'rtt_noted', False) and machine.rtt_ms else None
        # one statement whether the hour has a row yet or not: an upsert that adds to what is there
        table = MachineHour._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO %s (box, hour, games, heard, rtt_sum, rtt_n) VALUES (%%s, %%s, %%s, %%s, %%s, %%s) '
                'ON CONFLICT (box, hour) DO UPDATE SET games = games + excluded.games, heard = heard + excluded.heard, '
                'rtt_sum = rtt_sum + excluded.rtt_sum, rtt_n = rtt_n + excluded.rtt_n' % table,
                [box_of(machine), connection.ops.adapt_datetimefield_value(start_of(now)), games, heard, measured or 0.0, 1 if measured else 0])
    except Exception:
        log.exception("keeping the hour of %s failed", getattr(machine, 'id', '?'))


def prune(now=None):
    """Delete the hours older than KEEP_DAYS; how many went."""
    now = now or timezone.now()
    return MachineHour.objects.filter(hour__lt=now - datetime.timedelta(days=KEEP_DAYS)).delete()[0]


def last_hours(box, now, count=48):
    """The last `count` hours of a box, oldest first, ending with the one `now` is in:
    [{ hour, games, heard, rtt (ms or None), has }], one query, an empty hour a row of zeros with has=False."""
    end = start_of(now)
    first = end - HOUR * (count - 1)
    found = {row.hour: row for row in MachineHour.objects.filter(box=box, hour__gte=first, hour__lte=end)}
    out = []
    for i in range(count):
        hour = first + HOUR * i
        row = found.get(hour)
        out.append({'hour': hour, 'has': row is not None, 'games': row.games if row else 0, 'heard': row.heard if row else 0,
                    'rtt': row.rtt_sum / row.rtt_n if row and row.rtt_n else None})
    return out
