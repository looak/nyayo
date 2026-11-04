"""Worker registration: a worker says hello with its name and no credentials, is given a token, and is `pending` until the
owner approves it (or it comes with a join code). Only the token's hash is kept. See ramani plans/nyayo-2.md, *Worker
registration*, and docs/spec.md, *Lab: worker registration*.

States: `pending` (said hello, not approved: told what to describe, then to wait), `approved` (tests; `enabled` False
lets a workload it has finish and hands it no more) and `turned_away` (told to wait, and a new token is refused by its
name). At most PENDING_LIMIT workers are pending at once, and one with no approval for PENDING_DAYS is removed."""

import datetime
import hashlib
import secrets

from django.db import transaction
from django.utils import timezone

from nyayo.lab.models import JoinCode, LogEvent, Machine

PENDING_LIMIT = 20
PENDING_DAYS = 7
CODE_HOURS_DEFAULT, CODE_HOURS_MAX = 24, 24 * 14
STATES = ('pending', 'approved', 'turned_away')


class Refused(Exception):
    """Hello that cannot be answered with a token: `message`, `status` (HTTP) and the `state` it gets, if any."""

    def __init__(self, message, status=403, state=''):
        super().__init__(message)
        self.message, self.status, self.state = message, status, state


def digest(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def clean_name(value):
    value = ' '.join(str(value or '').split())[:100]
    return '' if value.lower() == 'none' else value


def by_token(token):
    """The worker that was given this token, or None."""
    if not isinstance(token, str) or not token:
        return None
    return Machine.objects.filter(token_hash=digest(token)).first()


def purge_pending(now=None):
    """Removes the workers that have been pending for PENDING_DAYS with no approval. Returns how many."""
    cutoff = (now or timezone.now()) - datetime.timedelta(days=PENDING_DAYS)
    stale = Machine.objects.filter(state='pending', first_seen__lt=cutoff, machine__isnull=True)
    count = stale.count()
    stale.delete()
    return count


def unique_name(wanted):
    """The name as a worker asked for it, or with (2), (3)... when another worker has it, compared without case."""
    base = clean_name(wanted) or 'worker'
    name, number = base, 1
    taken = {n.lower() for n in Machine.objects.values_list('name', flat=True)}
    while name.lower() in taken:
        number += 1
        suffix = ' (%d)' % number
        name = base[:100 - len(suffix)] + suffix
    return name


def make_code(made_by='', hours=CODE_HOURS_DEFAULT):
    """A join code, to be shown once: (code, JoinCode). Only its hash is kept."""
    hours = max(1, min(int(hours), CODE_HOURS_MAX))
    code = secrets.token_urlsafe(12)
    row = JoinCode.objects.create(code_hash=digest(code), made_by=made_by[:128],
                                  expires=timezone.now() + datetime.timedelta(hours=hours))
    return code, row


def use_code(code, now=None):
    """The JoinCode a code is, once; None when it is unknown, used or expired."""
    if not isinstance(code, str) or not code:
        return None
    row = JoinCode.objects.select_for_update().filter(code_hash=digest(code), used_by__isnull=True,
                                                      expires__gt=now or timezone.now()).first()
    return row


def hello(name, join_code='', token=''):
    """A worker says hello. (machine, token or None): the token is given once, on the hello that made the row; a hello
    with a token already known makes nothing. Raises Refused."""
    known = by_token(token)
    if known is not None:
        return known, None

    purge_pending()
    name = clean_name(name)
    if name and Machine.objects.filter(name__iexact=name, state='turned_away').exists():
        raise Refused('This name has been turned away', 403, 'turned_away')

    with transaction.atomic():
        row = None
        if join_code:
            row = use_code(join_code)
            if row is None:
                raise Refused('Bad join code', 403)
        elif Machine.objects.filter(state='pending').count() >= PENDING_LIMIT:
            raise Refused('Too many workers are waiting for approval', 429)
        secret = secrets.token_urlsafe(32)
        machine = Machine.objects.create(name=unique_name(name), token_hash=digest(secret), info={},
                                         state='approved' if row else 'pending')
        if row is not None:
            JoinCode.objects.filter(pk=row.pk).update(used_by=machine)
            log(machine, 'JOINED with a join code')
        else:
            log(machine, 'HELLO, waiting for approval')
    return machine, secret


def log(machine, summary, who='worker'):
    """Every change to a worker is an event, with who made it."""
    LogEvent.objects.create(author=who[:128], summary=('%s: %s' % (machine.name, summary))[:128], log_file='',
                            machine_id=machine.id, test_id=0)


def act(machine, action, who):
    """What the owner does on the Machines page: approve, turn_away, enable, disable. Returns the sentence for the person,
    or raises ValueError for what cannot be done to a worker in its state."""
    if action == 'approve':
        if machine.state == 'approved':
            raise ValueError('%s is approved already.' % machine.name)
        machine.state = 'approved'
        message = '%s was approved.' % machine.name
    elif action == 'turn_away':
        if machine.state == 'turned_away':
            raise ValueError('%s is turned away already.' % machine.name)
        machine.state = 'turned_away'
        message = '%s was turned away.' % machine.name
    elif action in ('enable', 'disable'):
        if machine.state != 'approved':
            raise ValueError('%s is not approved, so there is nothing to switch.' % machine.name)
        machine.enabled = action == 'enable'
        message = '%s was %sd.' % (machine.name, action)
    else:
        raise ValueError('Unknown action.')
    Machine.objects.filter(pk=machine.pk).update(state=machine.state, enabled=machine.enabled)
    log(machine, action.upper(), who)
    return message
