"""A failed start that deletes itself (docs/match-schema.md, *Expiry*).

A test whose engine fails before any game is played is reported as `failed_prechecks`, and what that leaves in nyayo is
never more than a failed start: the test and its logs stay on OpenBench. The worker looks once an hour
(`worker.run`) and deletes one when all of these hold, the time being the one the page shows (`stepsview.goes_at`,
`finished_at` plus NYAYO_FAILED_PRECHECKS_HOURS):

- its state is `failed_prechecks`, and it has a `finished_at` (without one nothing says when it ended);
- no result of it counts a game, and it has no game row, no schedule entry, no upload and no batch.

Anything else is left alone, and so are all the other states: a waiting or held test never expires. Deleting removes the
match's rows and its archive (`archive/matches/<id>.json` and `.meta.json`), or a rebuild would bring it back. A test
sent again keeps its old `finished_at`, so it goes on the next look; one restarted by hand isn't `failed_prechecks` any
more, and is new again.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from . import games, stepsview
from .ingest import delete_archived
from .models import Match

log = logging.getLogger("nyayo.expiry")


def held_data(match):
    """What a failed start doesn't have, if the match has it: games in its results, game rows, schedule entries,
    uploads or batches. Returns a short reason, or None."""
    if (match.results.aggregate(total=Sum("games"))["total"] or 0) > 0:
        return "its results count games"
    for name in ("games", "scheduled", "uploads", "batches"):
        if getattr(match, name).exists():
            return f"it has {name}"
    return None


def due(match, now):
    """Whether this match is a failed start whose time has come. The same time as the page's `stepsview.goes_at`."""
    at = stepsview.goes_at(match)
    return at is not None and at <= now and held_data(match) is None


def expire(now=None):
    """Deletes every failed start that is due. Returns the ids deleted."""
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.NYAYO_FAILED_PRECHECKS_HOURS)
    candidates = Match.objects.filter(state="failed_prechecks", finished_at__isnull=False,
                                      finished_at__lte=cutoff).values_list("pk", flat=True)
    gone = []
    for match_id in list(candidates):
        # looked at again under the database's write lock, which a producer's report waits for, so a test restarted
        # meanwhile (no longer failed_prechecks) or one that got games is not deleted
        with transaction.atomic():
            match = Match.objects.filter(pk=match_id).first()
            if match is None or not due(match, now):
                continue
            delete_archived(match_id)
            games.delete_files(match_id)
            match.delete()
        gone.append(match_id)
        log.info("expired %s: a failed start, ended %s", match_id, match.finished_at.isoformat())
    return gone
