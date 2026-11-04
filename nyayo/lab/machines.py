"""What the Machines pages show: a row for each worker, and for each token that reports without being one.

A `lab.Machine` is a worker, one row across its restarts (lab/registration.py), found by the token it was given on hello,
so there is nothing to group. It is joined by name to the token of the same name (`runs.Machine`), which is what reports
runs and matches. Its `state` is pending, approved or turned_away, and an approved one is enabled or not.

Only what `fields()` lists is ever read out of `Machine.info`, never the whole of it, and never `Machine.secret` or the
token's hash: the pages are open to read.
"""

import datetime
from collections import defaultdict

from django.db.models import Max, Sum
from django.utils import timezone

from nyayo.lab import cpuflags, toolchains, steps as walk
from nyayo.lab.ids import match_id
from nyayo.lab.models import EngineConfig, Machine, Place, Result, Step, Test
from nyayo.runs.models import Machine as Token

# A worker is on a test, or idle, while it was heard from this recently: the meaning of `active` in a match's document
ACTIVE = datetime.timedelta(minutes=5)


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


def os_kind(os_name, os_ver):
    """('linux', False), ('windows', False), ('macos', False), or ('', False); WSL is Linux under Windows, said by its
    kernel's release."""
    name = (os_name or '').lower()
    kind = 'macos' if name in ('darwin', 'macos') else name if name in ('linux', 'windows') else ''
    return kind, kind == 'linux' and 'microsoft' in (os_ver or '').lower() and 'wsl' in (os_ver or '').lower()


def megabytes(value):
    return ('%.1f GB (%s MB)' % (value / 1024, format(int(value), ','))) if isinstance(value, (int, float)) and value > 0 else ''


def gpus_of(info):
    """What the worker said of its graphics cards, a list of names or of objects with a name: [(name, detail)]."""
    found = []
    for gpu in info.get('gpus') if isinstance(info.get('gpus'), list) else []:
        if isinstance(gpu, str) and gpu.strip():
            found.append((gpu.strip(), ''))
        elif isinstance(gpu, dict):
            name = next((str(gpu[k]).strip() for k in ('name', 'model', 'device') if gpu.get(k)), '')
            detail = ' · '.join(str(gpu[k]) for k in ('vendor', 'driver', 'vram', 'memory') if gpu.get(k))
            if name:
                found.append((name, detail))
    return found


def fields(machine, configs=None):
    """A worker's info, by what is safe to show, as the pages read it."""
    info = machine.info if isinstance(machine.info, dict) else {}
    flags = [f for f in info.get('cpu_flags') or [] if isinstance(f, str)]
    shown = lambda *pairs: [(label, value) for label, value in pairs if value not in (None, '', 'None')]
    cores = '%s physical / %s logical' % (info.get('physical_cores'), info.get('logical_cores')) \
        if info.get('physical_cores') is not None and info.get('logical_cores') is not None else ''
    described = info.get('toolchains') if isinstance(info.get('toolchains'), dict) else {}
    legacy = info.get('compilers') if isinstance(info.get('compilers'), dict) else {}
    gpus = gpus_of(info)
    kind, wsl = os_kind(info.get('os_name'), info.get('os_ver'))
    per_engine = []
    for config in (configs if configs is not None else EngineConfig.objects.filter(enabled=True).order_by('name')):
        per_engine.append((config.name, toolchains.compiler_of(info, config.build()) or
                           (' '.join(str(p) for p in legacy[config.name]) if isinstance(legacy.get(config.name), (list, tuple)) else str(legacy.get(config.name) or ''))))
    return {
        'name': machine.name, 'os': info.get('os_name') or '', 'os_ver': info.get('os_ver') or '',
        'os_kind': kind, 'wsl': wsl,
        'cpu': info.get('cpu_name') or '', 'isa': info.get('isa_name') or '',
        'threads': info.get('concurrency'), 'sockets': info.get('sockets'),
        'logical': info.get('logical_cores'), 'physical': info.get('physical_cores'),
        'client': info.get('client_ver'), 'flags': flags,
        'compilers': {engine: found for engine, found in per_engine if found},
        # only the engines a worker has a token for, never the tokens
        'token_engines': sorted(info['tokens']) if isinstance(info.get('tokens'), dict) else [],
        'supported': [e for e in info.get('supported') or [] if isinstance(e, str)],
        'sections': [
            ('Hardware', shown(('CPU', info.get('cpu_name')), ('Cores', cores), ('Threads in use', info.get('concurrency')),
                               ('Sockets', info.get('sockets')), ('ISA', info.get('isa_name')))),
            ('Memory', shown(('RAM', megabytes(info.get('ram_total_mb'))), ('Huge pages', info.get('huge_pages')))),
            ('GPU', gpus or None),
            ('Software', shown(('System', ' '.join(p for p in (info.get('os_name'), info.get('os_ver')) if p)),
                               ('Python', info.get('python_ver')), ('Client version', info.get('client_ver')),
                               ('Fastchess version', info.get('fastchess_ver')), ('Syzygy WDL', info.get('syzygy_max')),
                               ('Command line', info.get('cli_options')))),
            ('Compilers', shown(*[(engine, found) for engine, found in per_engine if found],
                                *[(tool, str(described[tool])) for tool in toolchains.TOOLS if described.get(tool)],
                                ('C++ for fastchess', info.get('cxx_comp')))),
        ],
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
    else:
        why.extend(toolchains.can_build(info, build)[1])
    if info.get('os_name') not in build['systems']:
        why.append('%s is not one of %s' % (info.get('os_name') or 'The system', build['systems']))
    return why


def names():
    """The lowercase names of the boxes that have a page: every worker's and every token's."""
    found = {name.lower() for name in Machine.objects.values_list('name', flat=True) if name}
    return found | {name.lower() for name in Token.objects.values_list('name', flat=True)}


def totals():
    """{ machine id: (games, tests) } from the results."""
    out = defaultdict(lambda: [0, 0])
    for row in Result.objects.values('machine_id', 'test_id').annotate(games=Sum('games')):
        out[row['machine_id']][0] += row['games'] or 0
        out[row['machine_id']][1] += 1
    return out


def status(machine, now, tests):
    """What a worker is doing: 'pending', 'turned_away' or 'disabled' by the owner's say; else 'quiet' once not heard from for
    ACTIVE, 'playing' a test that has not ended (and its test), or 'idle'."""
    if machine.state == 'turned_away':
        return 'turned_away', None
    if machine.state == 'pending':
        return 'pending', None
    if machine.state == 'approved' and not machine.enabled:
        return 'disabled', None
    if now - machine.updated > ACTIVE:
        return 'quiet', None
    test = tests.get(machine.workload)
    return ('playing', test) if test else ('idle', None)


def trouble_for(machine_ids):
    """{ machine id: [what is wrong] } for the workers with a failed step the worker is blamed for, or one nobody can place
    on a test now held, on a test that has not ended."""
    out, held = defaultdict(list), {}
    for step in Step.objects.filter(result__machine_id__in=machine_ids, state='failed', blame__in=('worker', 'unclear'),
                                    result__test__finished=False).select_related('result', 'result__test', 'result__test__dev',
                                                                                 'result__test__base'):
        test, where = step.result.test, ('%s %s' % (step.step, step.side)).strip()
        if step.blame == 'worker':
            out[step.result.machine_id].append('%s failed on %s: %s' % (where, test_label(test), step.detail or 'no reason given'))
        else:
            if test.id not in held:
                held[test.id] = bool(walk.held(test))
            if held[test.id]:
                out[step.result.machine_id].append('%s is held after %s failed here' % (test_label(test), where))
    return out


def places():
    """{ lowercase name: where it is }"""
    return {place.name: place.text for place in Place.objects.all() if place.text}


def health(state, ago_text, problems, supports, configs, missing):
    """The status icon of a worker: offline (not heard from for ACTIVE, turned away or switched off), a warning (waiting for
    approval, or online with something wrong), or fine. The reasons are what its tooltip says, so the colour is never all
    of it."""
    if state == 'turned_away':
        return {'look': 'offline', 'label': 'Turned away', 'reasons': ['The owner turned this worker away; it is told to wait']}
    if state == 'pending':
        return {'look': 'warn', 'label': 'Waiting for approval', 'reasons': ['Said hello, and is told to wait until the owner approves it']}
    if state == 'disabled':
        return {'look': 'offline', 'label': 'Disabled', 'reasons': ['Switched off: it finishes a workload it has and gets no new one']}
    if state == 'quiet':
        return {'look': 'offline', 'label': 'Offline', 'reasons': ['Not heard from for 5 minutes (last %s)' % ago_text]}
    reasons = list(problems)
    if configs and not supports:
        reasons.append('Plays no engine: %s' % ' '.join('%s: %s.' % (name, '; '.join(why)) for name, why in missing))
    if reasons:
        return {'look': 'warn', 'label': 'Needs a look', 'reasons': reasons}
    return {'look': 'ok', 'label': 'Online', 'reasons': ['Heard from %s; nothing is wrong' % ago_text]}


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


ORDER = {'playing': 0, 'idle': 1, 'pending': 2, 'disabled': 3, 'quiet': 4, 'turned_away': 5, 'token': 6}


def listing(now=None):
    """Every worker and every token that is not one, as the list shows them: the playing first, a pending worker where it
    is seen, then the quiet, then tokens alone."""
    now = now or timezone.now()
    machines = list(Machine.objects.select_related('user'))
    totals_by = totals()
    workloads = {m.workload for m in machines if m.workload}
    tests = {t.id: t for t in Test.objects.filter(id__in=workloads, finished=False).select_related('dev', 'base')}
    configs = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    token_by = tokens(now)
    trouble, where = trouble_for([m.id for m in machines]), places()

    rows, used = [], set()
    for machine in machines:
        info = fields(machine, configs)
        state, test = status(machine, now, tests)
        supports = info['supported']
        token = token_by.get(machine.name.lower()) if machine.name else None
        if token:
            used.add(machine.name.lower())
        missing = [(c.name, lacks(machine.info, c)) for c in configs if c.name not in supports and lacks(machine.info, c)]
        games, played = totals_by[machine.id] if machine.id in totals_by else (0, 0)
        rows.append({
            'health': health(state, ago(machine.updated, now), trouble.get(machine.id, []), supports, configs, missing),
            'place': where.get(machine.name.lower(), ''), 'nameable': bool(machine.name),
            'rtt': None if state in ('quiet', 'turned_away', 'pending') else machine.rtt_ms,
            'os_kind': info['os_kind'], 'wsl': info['wsl'],
            'name': machine.name or 'machine %d' % machine.id, 'user': machine.user.username if machine.user_id else '',
            'os': info['os'], 'os_ver': info['os_ver'],
            'cpu': info['cpu'], 'threads': info['threads'], 'sockets': info['sockets'],
            'state': state, 'registration': machine.state, 'enabled': machine.enabled,
            'test': test, 'match': match_id(test.id) if test else '', 'label': test_label(test) if test else '',
            'dev_mnps': machine.dev_mnps, 'base_mnps': machine.base_mnps, 'updated': machine.updated, 'ago': ago(machine.updated, now),
            'first_seen': machine.first_seen, 'games': games, 'tests': played, 'supports': supports,
            'missing': missing, 'worker': True, 'token': token, 'id': machine.id,
        })
    for key, token in token_by.items():
        if key not in used:
            rows.append({'name': token['name'], 'worker': False, 'token': token, 'state': 'token', 'updated': token['last'], 'ago': token['ago'],
                         'games': 0, 'tests': 0, 'supports': [], 'missing': [], 'user': '', 'os': '', 'os_ver': '', 'cpu': '',
                         'threads': None, 'sockets': None, 'test': None, 'match': '', 'label': '', 'id': None, 'health': None,
                         'place': where.get(key, ''), 'nameable': True, 'rtt': None, 'os_kind': '', 'wsl': False,
                         'registration': '', 'enabled': True})
    rows.sort(key=lambda r: (ORDER[r['state']], -(r['updated'].timestamp() if r['updated'] else 0), r['name'].lower()))
    return rows


def find(name, now=None):
    """The worker or token called this (or `machine N` for the worker with that id), or None, with all that its page shows."""
    now = now or timezone.now()
    name = name.strip()
    machine = Machine.objects.select_related('user').filter(name__iexact=name).first()
    if machine is None and name.lower().startswith('machine ') and name[8:].isdigit():
        machine = Machine.objects.select_related('user').filter(id=int(name[8:])).first()
    token = tokens(now).get((machine.name if machine else name).lower())
    if machine is None and token is None:
        return None
    row = next((r for r in listing(now) if (machine and r['id'] == machine.id) or (not machine and r['name'].lower() == name.lower())), None)
    page = {'row': row, 'name': row['name'] if row else name, 'token': token, 'worker': machine is not None}
    if machine is None:
        page.update(place=places().get(name.lower(), ''), nameable=True)
        return page

    configs = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    info = fields(machine, configs)
    page.update(place=places().get(machine.name.lower(), ''), nameable=bool(machine.name),
                info=info, machine=machine, ago=ago(machine.updated, now),
                rtt=row['rtt'] if row else None,
                supported=[(c.name, c.name in info['supported'], lacks(machine.info, c), info['compilers'].get(c.name, '')) for c in configs])

    results = list(Result.objects.filter(machine_id=machine.id).select_related('test', 'test__dev', 'test__base').order_by('-updated'))
    recent = []
    for result in results[:20]:
        recent.append({'test': result.test, 'label': test_label(result.test), 'match': match_id(result.test_id), 'games': result.games,
                       'crashes': result.crashes, 'timeloss': result.timeloss, 'workloads': result.workloads,
                       'updated': result.updated, 'result_id': result.id, 'failures': 0})
    failed = defaultdict(int)
    for step in Step.objects.filter(result_id__in=[t['result_id'] for t in recent], state='failed'):
        failed[step.result_id] += 1
    for t in recent:
        t['failures'] = failed[t['result_id']]
    page['recent'] = recent
    page['pills'] = cpuflags.pills(info['flags'])

    # trouble now: a failed step the worker itself is blamed for (or unclear) on a test that has not ended
    trouble = []
    for step in Step.objects.filter(result_id__in=[r.id for r in results if not r.test.finished], state='failed',
                                    blame__in=('worker', 'unclear')).select_related('result', 'result__test'):
        trouble.append({'step': step, 'where': '%s %s' % (step.step, step.side) if step.side else step.step,
                        'match': match_id(step.result.test_id), 'label': test_label(step.result.test)})
    page['trouble'] = trouble
    return page
