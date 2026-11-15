"""The parameters an engine exposes at a commit, read from its tune build by a worker (docs/spec.md, *Lab: reading a tune's
parameters*).

nyayo never builds an engine. A page that wants the list of a commit queues a `TunableList` row (one per engine, commit and make
words); a worker that is asked for work is handed it ahead of a workload, builds the commit with the engine's `tune_make_args`,
asks the binary and reports the lines. A commit's list never changes, so a done row is kept for good; a failed one can be queued
again. The worker's half is `nyayo/client/tunables.py`; the lines are checked again here, as the client's word is not trusted.
"""

import datetime
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import github, makeargs
from .models import EngineConfig, TunableList

# A row that a worker took and never reported (it died, or lost the network) is queued again after this
STALE_MINUTES = 20

# The options every engine has, which are never a parameter to tune; the option names an engine's presets set are added to them
ORDINARY = ('Threads', 'Hash', 'Move Overhead', 'MoveOverhead', 'MultiPV', 'Ponder', 'Clear Hash', 'SyzygyPath', 'SyzygyProbeDepth',
            'Syzygy50MoveRule', 'SyzygyProbeLimit', 'EvalFile', 'OwnBook', 'Contempt', 'Slow Mover', 'nodestime', 'Skill Level')

COMMANDS = ('spsa', 'uci')
NUMBER = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'


def ordinary_options(config):
    """The names a worker leaves out of an answer to `uci`: the standard ones, and those the engine's presets name."""
    names = set(ORDINARY)
    for group in (config.presets or {}).values():
        for preset in (group or {}).values():
            for key in ('dev_options', 'base_options'):
                for word in str((preset or {}).get(key, '')).split():
                    names.add(word.partition('=')[0])
    return sorted(names)


def check_lines(text):
    """(lines, problem): the parameters in a worker's report, one `name, int|float, value, min, max, c_end, r_end` a line,
    normalised, or None and what is wrong."""
    lines, seen = [], set()
    for position, line in enumerate(str(text or '').splitlines(), 1):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(',')]
        if len(parts) != 7 or not re.fullmatch(r'[^\s=,]+', parts[0]) or parts[1] not in ('int', 'float') or not all(
                re.fullmatch(NUMBER, part) for part in parts[2:]):
            return None, 'line %d is not name, int|float, value, min, max, c_end, r_end' % position
        if parts[0] in seen:
            return None, '%s is in the list twice' % parts[0]
        seen.add(parts[0])
        lines.append(', '.join(parts))
    if not lines:
        return None, 'there are no parameters in it'
    return lines, None


def parse(row):
    """The list of a done row as dicts, in order: name, type, value, min, max, c_end, r_end."""
    keys = ('name', 'type', 'value', 'min', 'max', 'c_end', 'r_end')
    return [dict(zip(keys, (part.strip() for part in line.split(',')))) for line in row.lines.splitlines() if line.strip()]


def source_of(repo, sha):
    from . import utils
    return utils.path_join(github.api_base(repo), 'zipball', sha)


def request(config, sha, branch='', retry=False, now=None):
    """The row for this engine and commit, queued when it was not there, or when it failed and `retry` (a failure is read again
    on request only). A queued or reading row is left as it is, so that a commit is read once however many ask. Returns
    (row, queued): whether this call queued a read."""
    now = now or timezone.now()
    words = ' '.join(makeargs.words(config.tune_make_args))
    row = TunableList.objects.filter(engine=config.name, sha=sha, make_args=words).first()
    if row is None:
        try:
            with transaction.atomic():
                return TunableList.objects.create(engine=config.name, sha=sha, make_args=words, branch=branch[:255],
                                                  source=source_of(config.source, sha), queued=now), True
        except IntegrityError:
            row = TunableList.objects.get(engine=config.name, sha=sha, make_args=words)
    if row.state == TunableList.State.FAILED and retry:
        TunableList.objects.filter(pk=row.pk).update(state=TunableList.State.QUEUED, queued=now, started=None, read=None, detail='',
                                                     machine=None, branch=branch[:255] or row.branch)
        row.refresh_from_db()
        return row, True
    return row, False


def requeue_stale(now=None):
    """A read that a worker started and never reported goes back in the queue."""
    cutoff = (now or timezone.now()) - datetime.timedelta(minutes=STALE_MINUTES)
    return TunableList.objects.filter(state=TunableList.State.READING, started__lt=cutoff).update(
        state=TunableList.State.QUEUED, machine=None, started=None, detail='')


def next_job(machine, now=None):
    """The job a worker is handed ahead of a workload, or None: a queued read of an engine it supports. The row is marked as
    being read by it, in the same step, so that two workers don't read one commit."""
    if machine.state != 'approved' or not machine.enabled:
        return None
    # A worker polls often: with nothing queued or being read, this is one query that writes nothing
    if not TunableList.objects.filter(state__in=(TunableList.State.QUEUED, TunableList.State.READING)).exists():
        return None
    now = now or timezone.now()
    requeue_stale(now)
    supported = (machine.info or {}).get('supported', [])
    for row in TunableList.objects.filter(state=TunableList.State.QUEUED, engine__in=supported).order_by('queued', 'id'):
        config = EngineConfig.objects.filter(name=row.engine).first()
        if config is None or not config.enabled:
            continue
        taken = TunableList.objects.filter(pk=row.pk, state=TunableList.State.QUEUED).update(
            state=TunableList.State.READING, machine=machine, worker=machine.name, started=now, detail='')
        if taken:
            return job_of(row, config)
    return None


def job_of(row, config):
    """What the worker is told: the commit's zip, how to build it, and which options are not tunable."""
    return {
        'id': row.id, 'engine': config.name, 'sha': row.sha, 'name': row.branch or row.sha[:7], 'source': row.source,
        'private': config.private, 'build': config.build(), 'make_args': makeargs.words(row.make_args),
        'ordinary': ordinary_options(config),
    }


def report(machine, row_id, state, lines='', command='', error='', detail='', now=None):
    """What a worker says of a read it was given. (row, problem): the problem is plain text for the worker, or None. A row
    already done is left alone, as a commit's list never changes."""
    row = TunableList.objects.filter(pk=row_id).first()
    if row is None:
        return None, 'There is no such read'
    if row.state == TunableList.State.DONE:
        return row, None
    if row.state != TunableList.State.READING or row.machine_id != machine.id:
        return row, 'This read is not yours'
    now = now or timezone.now()
    if state == 'active':
        TunableList.objects.filter(pk=row.pk).update(detail=str(detail)[:500])
    elif state == 'done':
        checked, problem = check_lines(lines)
        if problem or command not in COMMANDS:
            problem = problem or 'The command that answered is spsa or uci'
            TunableList.objects.filter(pk=row.pk).update(state=TunableList.State.FAILED, error='The list from the worker was not usable: %s' % problem,
                                                         read=now, detail='')
            return TunableList.objects.get(pk=row.pk), problem
        TunableList.objects.filter(pk=row.pk).update(state=TunableList.State.DONE, lines='\n'.join(checked), command=command, error='',
                                                     read=now, detail='')
    elif state == 'failed':
        TunableList.objects.filter(pk=row.pk).update(state=TunableList.State.FAILED, error=str(error)[-4000:] or 'The worker gave no reason',
                                                     read=now, detail='')
    else:
        return row, 'Unknown state'
    return TunableList.objects.get(pk=row.pk), None


def of(config, sha):
    """The row of an engine at a commit, or None."""
    words = ' '.join(makeargs.words(config.tune_make_args))
    return TunableList.objects.filter(engine=config.name, sha=sha, make_args=words).first()


def facts(row):
    """What a page is told of a row, as JSON: the state, and for a done one the lines and which command answered."""
    if row is None:
        return {'state': 'none'}
    out = {'state': row.state, 'sha': row.sha, 'short': row.sha[:7], 'branch': row.branch}
    if row.state == TunableList.State.DONE:
        out.update(lines=row.lines, command=row.command, read=row.read.isoformat() if row.read else '', worker=row.worker,
                   guessed=row.command == 'uci')
    elif row.state == TunableList.State.READING:
        out.update(worker=row.worker, detail=row.detail)
    elif row.state == TunableList.State.FAILED:
        out.update(error=row.error, worker=row.worker)
    return out
