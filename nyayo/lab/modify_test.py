
# Adapted for nyayo from OpenBench's OpenBench/workloads/modify_workload.py (openbench_eg da629d7): stop, restart and
# approve. Delete, restore and modify are not on the page yet. Who may act is nyayo/actions/access.py's business; the
# one thing decided here is that approving is for superusers, who are the approvers OpenBench's Profile had.

import re
from importlib import import_module

from django.conf import settings
from django.db.models import Sum

from .models import LogEvent, Result, Test

ACTIONS = ('stop', 'restart', 'approve', 'unapprove')

OPENBENCH_ID = re.compile(r'^openbench-(?P<instance>.+)-(?P<number>\d+)$')


def test_of(match):

    # The lab's test a match is the document of, or None: an old match, from OpenBench's own server, has none.
    found = OPENBENCH_ID.match(match.pk)
    if not found or found['instance'] != settings.NYAYO_INSTANCE:
        return None
    return Test.objects.filter(pk=int(found['number'])).first()


def games_played(test):
    return (Result.objects.filter(test=test).aggregate(n=Sum('games'))['n'] or 0) + (test.games or 0)

def report(test):

    # Every change to a test is reported to nyayo's own match, once it is committed. A failure is a bug in one process:
    # it is raised, not queued. nyayo.lab.document is step 2's; imported here so that this page needs only the name.
    import_module('nyayo.lab.document').report_test(test)

def may(user, action):
    return user.is_superuser if action in ('approve', 'unapprove') else True

def apply(user, test, action):

    # Make the change; record the change; save the change. Returns what to tell the person.
    if action == 'approve':
        test.approved = True
        message = 'Test was approved.'

    elif action == 'unapprove':
        test.approved = False # A worker on it is told to stop at its next report or heartbeat (utils.update_test, clientHeartbeat)
        message = 'Test was unapproved.'

    elif action == 'restart':
        import_module('nyayo.lab.steps').restart_walks(test) # Every worker's walk starts over; a hold is released
        test.finished = False
        message = 'Test was restarted.'

    else:
        test.finished = True
        message = 'Test was stopped.'

    LogEvent.objects.create(author=user.get_username(), summary=action.upper(), log_file='', test_id=test.id)
    test.save()
    return message

def offers(test):

    # The buttons a test has now: stop while it runs, restart once it has stopped, approve until someone has
    out = []
    if not test.approved: out.append('approve')
    elif not test.finished and not games_played(test): out.append('unapprove') # With games played, stop is the way
    if test.finished or test.passed or test.failed: out.append('restart')
    if not test.finished: out.append('stop')
    return out
