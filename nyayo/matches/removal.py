"""Taking a match away: *Remove* (hide it, keep its games) and *Delete with its games* (nothing left but a tombstone).

**Remove** sets `Match.hidden`: the match and its test leave the lists, the dashboard and the live count, and its page is a
404 that says it was removed. Its games stay, in Games and the exports, and their pages work, saying they are from a removed
match. *Bring it back* clears the flag. A running test is stopped first; a producer that goes on reporting goes on
updating the match, which stays removed.

**Delete with games** removes the match's rows (results, schedule, snapshots, games), its batches with their cards, its
uploads and their files, its game files and its archive, and the lab's test with its results, steps and events. Nothing
brings it back, a rebuild from the archive included. A `Tombstone` (id, name, day) is kept so a link says *deleted*. A
match whose upload is still being read waits: `blockers` says so. Test ids are not reused (SQLite's AUTOINCREMENT).

A producer that sends a deleted match again makes it anew.
"""

import logging

from django.db import transaction

from . import catalogue, games
from .ingest import delete_archived
from .models import Match, Tombstone, Upload

log = logging.getLogger("nyayo.removal")


def blockers(matches):
    """Why these matches cannot be taken away yet, as sentences (empty when they can): an upload that is still being read
    would be put into batches of a match that is gone."""
    return ["%s has an upload still being processed (%s). Wait for the worker to finish it." % (name_of(m), u.name)
            for m in matches for u in Upload.objects.filter(match=m, state="pending")]


def name_of(match):
    return match.name or match.pk


def stop_test(match, user):
    """Stop the lab's test of a match that is still running, and report it: no worker gets more of it. Returns whether it did."""
    from nyayo.lab import modify_test

    test = modify_test.test_of(match)
    if test is None or test.finished:
        return False
    modify_test.apply(user, test, "stop")
    modify_test.report(test)
    return True


def remove(matches, user):
    """Hide the matches, stopping any test that is running. Returns the ids."""
    done = []
    for match in matches:
        stop_test(match, user)
        Match.objects.filter(pk=match.pk).update(hidden=True)
        done.append(match.pk)
        log.info("%s removed %s", user.get_username(), match.pk)
    return done


def bring_back(matches):
    ids = [m.pk for m in matches]
    Match.objects.filter(pk__in=ids).update(hidden=False)
    return ids


def delete(matches, user):
    """Delete the matches with everything they keep. Returns the ids. Call `blockers` first."""
    from nyayo.lab import modify_test
    from nyayo.lab.models import LogEvent, Result, Test

    done = []
    for match in matches:
        stop_test(match, user)
        test = modify_test.test_of(match)
        match_id, name = match.pk, name_of(match)
        catalogue.purge(match_id)            # batches (files, rows, cards) and uploads (files, rows)
        games.delete_files(match_id)         # the games being played, as PGNs
        delete_archived(match_id)            # or a rebuild would bring it back
        with transaction.atomic():
            Match.objects.filter(pk=match_id).delete()   # results, schedule, snapshots and game rows go with it
            if test is not None:
                Result.objects.filter(test=test).delete()    # and their steps
                LogEvent.objects.filter(test_id=test.id).delete()
                test.delete()
            Tombstone.objects.update_or_create(pk=match_id, defaults={"name": name if name != match_id else ""})
        done.append(match_id)
        log.info("%s deleted %s with its games", user.get_username(), match_id)
    return done
