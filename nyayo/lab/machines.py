"""What the Machines pages show: one row per box, however many times its worker has started.

OpenBench's server makes a new `lab.Machine` for every session a worker starts (`clientWorkerInfo`), so a box that was
restarted ten times has ten rows. Here they are grouped by the worker's name (`-I`, which the document calls
`machine_name`), or, for one started without a name, by its user and MAC address, and the newest session stands for the
box: its status, its speed, its system. The totals are those of every session. A box is joined by name to the token of
the same name (`runs.Machine`), which is what reports runs and matches.

Only what is listed in `SYSTEM` and `fields()` is ever read out of `Machine.info`, never the whole of it, and never
`Machine.secret`: the pages are open to read.
"""

import datetime
from collections import defaultdict

from django.db.models import Count, Max, Sum
from django.utils import timezone

from nyayo.lab.ids import match_id
from nyayo.lab.models import EngineConfig, Machine, Result, Step, Test
from nyayo.runs.models import Machine as Token

# A worker is on a test, or idle, while it was heard from this recently: the meaning of `active` in a match's document
ACTIVE = datetime.timedelta(minutes=5)

NAME_KEY, MAC_KEY = 'info__machine_name', 'info__mac_address'

# What the detail page prints of a worker's system, in this order: (label, key in info)
SYSTEM = (
    ('Client version', 'client_ver'), ('Fastchess version', 'fastchess_ver'), ('C++ compiler', 'cxx_comp'),
    ('Python', 'python_ver'), ('CPU', 'cpu_name'), ('ISA', 'isa_name'), ('Logical cores', 'logical_cores'),
    ('Physical cores', 'physical_cores'), ('Total RAM (MB)', 'ram_total_mb'), ('Huge pages', 'huge_pages'),
    ('Syzygy WDL', 'syzygy_max'), ('Threads', 'concurrency'), ('Sockets', 'sockets'), ('Command line', 'cli_options'),
)


def ago(then, now):
    """just now, 3 min ago, 5 h ago, 6 days ago"""
    if not then:
        return ''
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return '%d min ago' % (seconds // 60)
    if seconds < 86400:
        return '%d h ago' % (seconds // 3600)
    days = seconds // 86400
    return '%d day%s ago' % (days, '' if days == 1 else 's')


def clean_name(value):
    value = (value or '').strip() if isinstance(value, str) else ''
    return '' if value == 'None' else value


def box_key(name, user_id, mac):
    return ('name', name.lower()) if name else ('anonymous', user_id, mac or '')


def fields(machine):
    """A session's info, by what is safe to show, as the pages read it."""
    info = machine.info if isinstance(machine.info, dict) else {}
    compilers = info.get('compilers') if isinstance(info.get('compilers'), dict) else {}
    flags = [f for f in info.get('cpu_flags') or [] if isinstance(f, str)]
    return {
        'name': clean_name(info.get('machine_name')), 'os': info.get('os_name') or '', 'os_ver': info.get('os_ver') or '',
        'cpu': info.get('cpu_name') or '', 'isa': info.get('isa_name') or '',
        'threads': info.get('concurrency'), 'sockets': info.get('sockets'),
        'logical': info.get('logical_cores'), 'physical': info.get('physical_cores'),
        'client': info.get('client_ver'), 'flags': flags,
        'compilers': {engine: ' '.join(str(part) for part in found) if isinstance(found, (list, tuple)) else str(found)
                      for engine, found in compilers.items()},
        # only the engines a worker has a token for, never the tokens
        'token_engines': sorted(info['tokens']) if isinstance(info.get('tokens'), dict) else [],
        'supported': [e for e in info.get('supported') or [] if isinstance(e, str)],
        'system': [(label, info[key]) for label, key in SYSTEM if info.get(key) not in (None, '', 'None')],
    }


def lacks(info, config):
    """Why a worker with this info cannot play an engine, by the rules `clientWorkerInfo` applies; empty when it can."""
    info = info if isinstance(info, dict) else {}
    build, why = config.build(), []
    flags = info.get('cpu_flags') or []
    missing = [flag for flag in build['cpuflags'] if flag not in flags]
    if missing:
        why.append('Missing CPU flags %s' % missing)
    if config.private:
        if config.name not in (info.get('tokens') or {}):
            why.append('No access token for %s' % config.name)
    elif config.name not in (info.get('compilers') or {}):
        why.append('Missing %s' % build['compilers'])
    if info.get('os_name') not in build['systems']:
        why.append('%s is not one of %s' % (info.get('os_name') or 'The system', build['systems']))
    return why


def names():
    """The lowercase names of the boxes that have a page: every worker's and every token's."""
    found = {clean_name(name).lower() for name in Machine.objects.values_list(NAME_KEY, flat=True).distinct()}
    found.discard('')
    return found | {name.lower() for name in Token.objects.values_list('name', flat=True)}


def groups():
    """{ key: { name, sessions, latest_id, ids } } over every worker session, from a query that groups in the
    database, so a box with a thousand sessions costs a row."""
    found = {}
    rows = Machine.objects.values('user_id', NAME_KEY, MAC_KEY).annotate(latest=Max('id'), n=Count('id'))
    for row in rows:
        name = clean_name(row[NAME_KEY])
        key = box_key(name, row['user_id'], row[MAC_KEY])
        box = found.setdefault(key, {'key': key, 'name': name, 'latest_id': 0, 'triples': [], 'sessions': 0})
        box['triples'].append((row['user_id'], name, row[MAC_KEY]))
        box['latest_id'] = max(box['latest_id'], row['latest'])
        box['sessions'] += row['n']
    return found


def totals():
    """{ triple: (games, tests) } from the results, one row per box and test."""
    pairs = defaultdict(lambda: [0, set()])
    for row in Result.objects.values('machine__user_id', 'machine__' + NAME_KEY, 'machine__' + MAC_KEY, 'test_id') \
            .annotate(games=Sum('games')):
        triple = (row['machine__user_id'], clean_name(row['machine__' + NAME_KEY]), row['machine__' + MAC_KEY])
        pairs[triple][0] += row['games'] or 0
        pairs[triple][1].add(row['test_id'])
    return pairs


def status(machine, now, tests):
    """('playing', test) on a test that has not ended, ('idle', None), or ('quiet', None) once not heard from for ACTIVE."""
    if now - machine.updated > ACTIVE:
        return 'quiet', None
    test = tests.get(machine.workload)
    return ('playing', test) if test else ('idle', None)


def test_label(test):
    return test.name or '%s vs %s' % (test.dev.name, test.base.name)


def tokens(now):
    """{ lowercase name: { token row and when it last reported } }"""
    out = {}
    for token in Token.objects.annotate(last_run=Max('runs__received_at'), last_match=Max('matches__received_at')):
        last = max((t for t in (token.last_run, token.last_match) if t), default=None)
        out[token.name.lower()] = {'name': token.name, 'active': token.active, 'created': token.created_at, 'last': last,
                                   'ago': ago(last, now), 'runs': token.last_run is not None, 'matches': token.last_match is not None}
    return out


def listing(now=None):
    """Every box, a worker's or a token's, as the list shows them: busiest first, then the quiet, then tokens alone."""
    now = now or timezone.now()
    boxes, totals_by = groups(), totals()
    latest = {m.id: m for m in Machine.objects.filter(id__in=[b['latest_id'] for b in boxes.values()]).select_related('user')}
    workloads = {m.workload for m in latest.values() if m.workload}
    tests = {t.id: t for t in Test.objects.filter(id__in=workloads, finished=False).select_related('dev', 'base')}
    configs = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    token_by = tokens(now)

    rows, used = [], set()
    for box in boxes.values():
        machine = latest[box['latest_id']]
        info = fields(machine)
        state, test = status(machine, now, tests)
        games = sum(totals_by[t][0] for t in box['triples'] if t in totals_by)
        played = set().union(*(totals_by[t][1] for t in box['triples'] if t in totals_by)) if totals_by else set()
        supports = info['supported']
        token = token_by.get(box['name'].lower()) if box['name'] else None
        if token:
            used.add(box['name'].lower())
        name = box['name'] or 'machine %d' % machine.id
        rows.append({
            'name': name, 'user': machine.user.username, 'os': info['os'], 'os_ver': info['os_ver'],
            'cpu': info['cpu'], 'threads': info['threads'], 'sockets': info['sockets'],
            'state': state, 'test': test, 'match': match_id(test.id) if test else '', 'label': test_label(test) if test else '',
            'dev_mnps': machine.dev_mnps, 'base_mnps': machine.base_mnps, 'updated': machine.updated, 'ago': ago(machine.updated, now),
            'games': games, 'tests': len(played), 'sessions': box['sessions'], 'supports': supports,
            'missing': [(c.name, lacks(machine.info, c)) for c in configs if c.name not in supports and lacks(machine.info, c)],
            'worker': True, 'token': token, 'id': machine.id,
        })
    for key, token in token_by.items():
        if key not in used:
            rows.append({'name': token['name'], 'worker': False, 'token': token, 'state': 'token', 'updated': token['last'], 'ago': token['ago'],
                         'games': 0, 'tests': 0, 'sessions': 0, 'supports': [], 'missing': [], 'user': '', 'os': '', 'os_ver': '', 'cpu': '',
                         'threads': None, 'sockets': None, 'test': None, 'match': '', 'label': '', 'id': None})
    order = {'playing': 0, 'idle': 1, 'quiet': 2, 'token': 3}
    rows.sort(key=lambda r: (order[r['state']], -(r['updated'].timestamp() if r['updated'] else 0), r['name'].lower()))
    return rows


def find(name, now=None):
    """The box called this (a name, or `machine N` for the session N of one with none), or None, with all that its
    page shows."""
    now = now or timezone.now()
    name = name.strip()
    boxes = groups()
    box = next((b for b in boxes.values() if b['name'] and b['name'].lower() == name.lower()), None)
    if box is None and name.lower().startswith('machine ') and name[8:].isdigit():
        session = Machine.objects.filter(id=int(name[8:])).first()
        if session:
            key = box_key(clean_name((session.info or {}).get('machine_name')), session.user_id, (session.info or {}).get('mac_address'))
            box = boxes.get(key)
    token = tokens(now).get(name.lower())
    if box is None and token is None:
        return None
    row = next((r for r in listing(now) if r['name'].lower() == name.lower() or (box and r['id'] == box['latest_id'])), None)
    page = {'row': row, 'name': row['name'] if row else name, 'token': token, 'worker': box is not None}
    if box is None:
        return page

    machine = Machine.objects.select_related('user').get(id=box['latest_id'])
    info = fields(machine)
    configs = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    page.update(info=info, machine=machine, ago=ago(machine.updated, now), supported=[(c.name, c.name in info['supported'], lacks(machine.info, c), info['compilers'].get(c.name, '')) for c in configs])

    # every session's results, newest test first
    ids = [i for i, user_id, name, mac in Machine.objects.values_list('id', 'user_id', NAME_KEY, MAC_KEY)
           if box_key(clean_name(name), user_id, mac) == box['key']]
    results = list(Result.objects.filter(machine_id__in=ids).select_related('test', 'test__dev', 'test__base').order_by('-updated'))
    by_test = {}
    for result in results:
        t = by_test.setdefault(result.test_id, {'test': result.test, 'label': test_label(result.test), 'match': match_id(result.test_id),
                                                'games': 0, 'crashes': 0, 'timeloss': 0, 'workloads': 0, 'updated': result.updated, 'result_ids': []})
        t['games'] += result.games
        t['crashes'] += result.crashes
        t['timeloss'] += result.timeloss
        t['workloads'] += result.workloads
        t['result_ids'].append(result.id)
    recent = list(by_test.values())[:20]
    steps = Step.objects.filter(result_id__in=[i for t in recent for i in t['result_ids']], state='failed').select_related('result')
    failed = defaultdict(list)
    for step in steps:
        failed[step.result.test_id].append(step)
    for t in recent:
        t['failures'] = len(failed[t['test'].id])
    page['recent'] = recent

    # trouble now: a failed step the worker itself is blamed for (or unclear) on a test that has not ended
    trouble = []
    for step in Step.objects.filter(result_id__in=[r.id for r in results if not r.test.finished], state='failed',
                                    blame__in=('worker', 'unclear')).select_related('result', 'result__test'):
        trouble.append({'step': step, 'where': '%s %s' % (step.step, step.side) if step.side else step.step,
                        'match': match_id(step.result.test_id), 'label': test_label(step.result.test)})
    page['trouble'] = trouble
    page['sessions'] = len(ids)
    return page
