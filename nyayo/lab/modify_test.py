
# Adapted for nyayo from OpenBench's OpenBench/workloads/modify_workload.py (openbench_eg da629d7): stop, restart and
# approve. Delete, restore and modify are not on the page yet. Who may act is nyayo/actions/access.py's business; the
# one thing decided here is that approving is for superusers, who are the approvers OpenBench's Profile had.

from importlib import import_module

from .models import LogEvent, Test

ACTIONS = ('stop', 'restart', 'approve')

def report(test):

    # Every change to a test is reported to nyayo's own match, once it is committed. A failure is a bug in one process:
    # it is raised, not queued. nyayo.lab.document is step 2's; imported here so that this page needs only the name.
    import_module('nyayo.lab.document').report_test(test)

def may(user, action):
    return user.is_superuser if action == 'approve' else True

def apply(user, test, action):

    # Make the change; record the change; save the change. Returns what to tell the person.
    if action == 'approve':
        test.approved = True
        message = 'Test was approved.'

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
    if test.finished or test.passed or test.failed: out.append('restart')
    if not test.finished: out.append('stop')
    return out
