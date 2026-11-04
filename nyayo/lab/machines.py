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
import math
from collections import defaultdict

from django.core.paginator import Paginator
from django.db.models import Count, Max, Sum
from django.utils import timezone

from nyayo.lab import cpuflags, hours, steps as walk
from nyayo.lab.ids import match_id
from nyayo.lab.models import EngineConfig, Machine, Place, Result, Step, Test
from nyayo.paging import PAGE_SIZE, paginate
from nyayo.runs.models import Machine as Token

# A worker is on a test, or idle, while it was heard from this recently: the meaning of `active` in a match's document
ACTIVE = datetime.timedelta(minutes=5)

# what a test's mode is called where a box's contribution is counted
KINDS = {'SPRT': 'SPRT', 'SPSA': 'SPSA', 'GAMES': 'games', 'DATAGEN': 'datagen'}

NAME_KEY, MAC_KEY = 'info__machine_name', 'info__mac_address'

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


def fields(machine):
    """A session's info, by what is safe to show, as the pages read it."""
    info = machine.info if isinstance(machine.info, dict) else {}
    compilers = info.get('compilers') if isinstance(info.get('compilers'), dict) else {}
    flags = [f for f in info.get('cpu_flags') or [] if isinstance(f, str)]
    shown = lambda *pairs: [(label, value) for label, value in pairs if value not in (None, '', 'None')]
    cores = '%s physical / %s logical' % (info.get('physical_cores'), info.get('logical_cores'))         if info.get('physical_cores') is not None and info.get('logical_cores') is not None else ''
    compilers = {engine: ' '.join(str(part) for part in found) if isinstance(found, (list, tuple)) else str(found)
                 for engine, found in compilers.items()}
    gpus = gpus_of(info)
    kind, wsl = os_kind(info.get('os_name'), info.get('os_ver'))
    return {
        'name': clean_name(info.get('machine_name')), 'os': info.get('os_name') or '', 'os_ver': info.get('os_ver') or '',
        'os_kind': kind, 'wsl': wsl,
        'cpu': info.get('cpu_name') or '', 'isa': info.get('isa_name') or '',
        'threads': info.get('concurrency'), 'sockets': info.get('sockets'),
        'logical': info.get('logical_cores'), 'physical': info.get('physical_cores'),
        'client': info.get('client_ver'), 'flags': flags, 'compilers': compilers,
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
            ('Compilers', shown(*[(engine, found) for engine, found in sorted(compilers.items())],
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


def trouble_for(machine_ids):
    """{ machine id: [what is wrong] } for the workers whose newest session has a failed step the worker is blamed for,
    or one nobody can place on a test now held, on a test that has not ended."""
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
    """The status icon of a worker: offline (not heard from for ACTIVE), a warning (online, but something is wrong), or
    fine. The reasons are what its tooltip says, so the colour is never all of it."""
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


def listing(now=None):
    """Every box, a worker's or a token's, as the list shows them: busiest first, then the quiet, then tokens alone."""
    now = now or timezone.now()
    boxes, totals_by = groups(), totals()
    latest = {m.id: m for m in Machine.objects.filter(id__in=[b['latest_id'] for b in boxes.values()]).select_related('user')}
    workloads = {m.workload for m in latest.values() if m.workload}
    tests = {t.id: t for t in Test.objects.filter(id__in=workloads, finished=False).select_related('dev', 'base')}
    configs = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    token_by = tokens(now)
    trouble, where = trouble_for(list(latest)), places()

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
        missing = [(c.name, lacks(machine.info, c)) for c in configs if c.name not in supports and lacks(machine.info, c)]
        rows.append({
            'health': health(state, ago(machine.updated, now), trouble.get(machine.id, []), supports, configs, missing),
            'place': where.get(box['name'].lower(), '') if box['name'] else '', 'nameable': bool(box['name']),
            'rtt': None if state == 'quiet' else machine.rtt_ms, 'os_kind': info['os_kind'], 'wsl': info['wsl'],
            'name': name, 'user': machine.user.username, 'os': info['os'], 'os_ver': info['os_ver'],
            'cpu': info['cpu'], 'threads': info['threads'], 'sockets': info['sockets'],
            'state': state, 'test': test, 'match': match_id(test.id) if test else '', 'label': test_label(test) if test else '',
            'dev_mnps': machine.dev_mnps, 'base_mnps': machine.base_mnps, 'updated': machine.updated, 'ago': ago(machine.updated, now),
            'games': games, 'tests': len(played), 'sessions': box['sessions'], 'supports': supports,
            'missing': missing, 'worker': True, 'token': token, 'id': machine.id,
        })
    for key, token in token_by.items():
        if key not in used:
            rows.append({'name': token['name'], 'worker': False, 'token': token, 'state': 'token', 'updated': token['last'], 'ago': token['ago'],
                         'games': 0, 'tests': 0, 'sessions': 0, 'supports': [], 'missing': [], 'user': '', 'os': '', 'os_ver': '', 'cpu': '',
                         'threads': None, 'sockets': None, 'test': None, 'match': '', 'label': '', 'id': None, 'health': None,
                         'place': where.get(key, ''), 'nameable': True, 'rtt': None, 'os_kind': '', 'wsl': False})
    order = {'playing': 0, 'idle': 1, 'quiet': 2, 'token': 3}
    rows.sort(key=lambda r: (order[r['state']], -(r['updated'].timestamp() if r['updated'] else 0), r['name'].lower()))
    return rows


LANGUAGES = {'g++': 'C++', 'clang++': 'C++', 'c++': 'C++', 'gcc': 'C', 'clang': 'C', 'cc': 'C', 'cl': 'C++', 'python': 'Python',
             'python3': 'Python', 'rustc': 'Rust', 'cargo': 'Rust', 'dotnet': 'C#', 'make': 'Build', 'cmake': 'Build', 'ninja': 'Build'}
LANGUAGE_CLASSES = {'C++': 'cpp', 'C': 'c', 'C#': 'cs', 'Rust': 'rust', 'Python': 'python', 'Build': 'build'}


def nice(top):
    """The next round number at or above `top` for an axis: 1, 1.2, 1.5, 2, 3, 4, 5, 6, 8 times a power of ten."""
    if top <= 0:
        return 1
    scale = 10 ** math.floor(math.log10(top))
    for step in (1, 1.2, 1.5, 2, 3, 4, 5, 6, 8, 10):
        if step * scale >= top - 1e-9:
            return step * scale
    return 10 * scale


def plain(number):
    return int(number) if number == int(number) else number


def duration(seconds):
    minutes = int(seconds // 60)
    return '%d h %d min' % divmod(minutes, 60) if minutes >= 60 else '%d min' % minutes


def shown_ms(value):
    return ('%.1f' % value) if value < 10 else '%d' % round(value)


def graph(rows, now, up):
    """The 48-hour graph of a box as the page draws it, from `hours.last_hours`: a pillar of games for each hour, the
    response time line (broken where there is none, and absent when no hour has one), and a strip of whether it was heard
    from. An hour before the first one on record is *blank*, not offline: a new box has nothing to show for the time before
    nyayo knew it, and an hour with no games is a quiet one, not a broken one."""
    first = next((i for i, r in enumerate(rows) if r['has']), None)
    top = nice(max([r['games'] for r in rows] + [4]))
    measured = [r['rtt'] for r in rows if r['rtt'] is not None]
    ceiling = nice(max(measured) * 1.3) if measured else None
    cols, segments, run, online = [], [], [], 0
    for i, r in enumerate(rows):
        ago_h = len(rows) - 1 - i
        when = timezone.localtime(r['hour']).strftime('%a %H:00')
        heard = r['heard']
        if first is None or i < first:
            look, says = 'none', 'nothing on record yet'
        elif heard >= 1800:
            look, says = 'on', 'online'
        elif heard > 0:
            look, says = 'part', 'heard from for %s' % duration(heard)
        else:
            look, says = 'off', 'offline' if r['has'] else 'offline, nothing heard'
        online += heard
        label = 'this hour' if ago_h == 0 else '1 h ago' if ago_h == 1 else '%d h ago' % ago_h
        bits = []
        if look != 'none':
            bits.append('%d game%s' % (r['games'], '' if r['games'] == 1 else 's'))
            if r['rtt'] is not None:
                bits.append('%s ms' % shown_ms(r['rtt']))
        bits.append(says)
        cols.append({'pct': round(r['games'] / top * 100, 1), 'look': look, 'games': r['games'],
                     'tip': '%s, %s: %s' % (when, label, ', '.join(bits))})
        if ceiling and r['rtt'] is not None:
            run.append((i * 10 + 5, round(100 - r['rtt'] / ceiling * 100, 1)))
        elif run:
            segments.append(run)
            run = []
    if run:
        segments.append(run)
    lines = [' '.join('%s,%s' % p for p in (seg if len(seg) > 1 else [(seg[0][0] - 3, seg[0][1]), (seg[0][0] + 3, seg[0][1])]))
             for seg in segments]

    last24 = rows[-24:]
    heard24 = sum(r['heard'] for r in last24)
    games24 = sum(r['games'] for r in last24)
    recorded = len(rows) - first if first is not None else 0
    span = (now - rows[first]['hour']).total_seconds() if first is not None else 0
    return {
        'cols': cols, 'lines': lines, 'top': plain(top), 'half': plain(top / 2),
        'rtt_top': plain(ceiling) if ceiling else None, 'rtt_half': plain(ceiling / 2) if ceiling else None, 'has_rtt': bool(measured),
        'recorded': recorded, 'empty': first is None, 'little': first is None or recorded < len(rows),
        'since': timezone.localtime(rows[first]['hour']) if first is not None else None,
        'games_48': sum(r['games'] for r in rows), 'average': round(sum(r['games'] for r in rows) / len(rows)),
        'up_for': duration(online) if first is not None else '', 'up_pct': min(100, round(online / span * 100)) if span > 0 else None,
        'per_hour': round(games24 / (heard24 / 3600)) if heard24 >= 600 else None,
        'rtt_now': measured[-1] if measured and up else None, 'up_now': up,
    }


def hardware(info):
    """The rows of the Hardware box: what the worker reported of its processor, memory and graphics cards."""
    sections = dict(info['sections'])
    rows = []
    if info['cpu']:
        rows.append(('CPU', info['cpu']))
    counted = []
    if info['physical'] is not None:
        counted.append('%s cores' % info['physical'])
    if info['logical'] is not None:
        counted.append('%s threads' % info['logical'])
    if info['sockets'] is not None:
        counted.append('%s socket%s' % (info['sockets'], '' if info['sockets'] == 1 else 's'))
    if counted or info['threads'] is not None:
        playing = ' · playing with %s' % info['threads'] if info['threads'] is not None else ''
        rows.append(('Cores', ', '.join(counted) + playing))
    if info['isa']:
        rows.append(('Instruction set', info['isa']))
    memory = ' · '.join(str(v) if label == 'RAM' else '%s huge pages' % v for label, v in sections.get('Memory') or [])
    rows.append(('Memory', memory or 'not reported'))
    gpus = sections.get('GPU') or []
    rows.append(('GPU', ', '.join('%s (%s)' % (name, detail) if detail else name for name, detail in gpus) or 'none reported'))
    return rows


def software(info, machine):
    """The rows of the Software box, the client's version said against the one the server wants."""
    from nyayo.lab import config
    rows = []
    for label, value in dict(info['sections']).get('Software') or []:
        if label == 'Client version':
            try:
                wanted = config.client_version()
                value = '%s, %s' % (value, 'up to date' if str(value) == str(wanted) else 'the server wants %s' % wanted)
            except Exception:
                pass
        rows.append((label, value))
    rows += [(label, value) for label, value in dict(info['sections']).get('Compilers') or [] if label == 'C++ for fastchess']
    return rows


def toolchains(machine):
    """{ rows: [{ lang, name, version, found }], complete } . Registration (step 12) records the toolchains a worker finds,
    found or not (a dict of name to version or empty, or a list of {name, version}); until a worker has them the list is
    what it reported with each engine (a compiler and its version) and its Python, and `complete` is False so the page
    says so."""
    kept = getattr(machine, 'toolchains', None)
    found = []
    if isinstance(kept, dict) and kept:
        found = [(str(name), str(version) if version else '') for name, version in kept.items()]
    elif isinstance(kept, list) and kept:
        found = [(str(item['name']), str(item.get('version') or '')) for item in kept if isinstance(item, dict) and item.get('name')]
    complete = bool(found)
    if not found:
        seen = {}
        for text in fields(machine)['compilers'].values():
            parts = text.split()
            if parts:
                seen.setdefault(parts[0], parts[1] if len(parts) > 1 else '')
        python = machine.info.get('python_ver') if isinstance(machine.info, dict) else ''
        if python:
            seen.setdefault('python3', str(python))
        found = list(seen.items())
    rows = []
    for name, version in found:
        lang = LANGUAGES.get(name.lower(), 'Tool')
        rows.append({'lang': lang, 'cls': LANGUAGE_CLASSES.get(lang, 'tool'), 'name': name, 'version': version, 'found': bool(version)})
    return {'rows': rows, 'complete': complete}


def approval(machine):
    """What registration (step 12) keeps of a worker's approval, or None while the model has none."""
    if not hasattr(machine, 'state'):
        return None
    return {'state': machine.state, 'enabled': getattr(machine, 'enabled', None)}


def find(name, now=None, request=None):
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
        page.update(place=places().get(name.lower(), ''), nameable=True)
        return page

    machine = Machine.objects.select_related('user').get(id=box['latest_id'])
    info = fields(machine)
    configs = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    page.update(place=places().get(box['name'].lower(), '') if box['name'] else '', nameable=bool(box['name']),
                info=info, machine=machine, ago=ago(machine.updated, now), rtt=None if row and row['state'] == 'quiet' else machine.rtt_ms, supported=[(c.name, c.name in info['supported'], lacks(machine.info, c), info['compilers'].get(c.name, '')) for c in configs])

    # every session's results: the totals and the kinds in a query each, the tests a page at a time
    ids = [i for i, user_id, name, mac in Machine.objects.values_list('id', 'user_id', NAME_KEY, MAC_KEY)
           if box_key(clean_name(name), user_id, mac) == box['key']]
    mine = Result.objects.filter(machine_id__in=ids)
    sums = mine.aggregate(games=Sum('games'), crashes=Sum('crashes'), timeloss=Sum('timeloss'), tests=Count('test', distinct=True))
    page['totals'] = {k: v or 0 for k, v in sums.items()}
    kinds = {row['test__test_mode']: row['n'] for row in mine.values('test__test_mode').annotate(n=Count('test', distinct=True))}
    page['kinds'] = [(KINDS.get(mode, mode), kinds[mode]) for mode in sorted(kinds, key=lambda m: -kinds[m])]
    last = mine.select_related('test', 'test__dev', 'test__base').order_by('-updated').first()
    page['last'] = None if last is None else {
        'label': test_label(last.test), 'match': match_id(last.test_id), 'kind': KINDS.get(last.test.test_mode, last.test.test_mode),
        'against': last.test.base.name, 'workloads': last.workloads, 'games': last.games, 'ago': ago(last.updated, now),
        'finished': last.test.finished}

    listing_rows = mine.values('test_id').annotate(games=Sum('games'), crashes=Sum('crashes'), timeloss=Sum('timeloss'),
                                                  workloads=Sum('workloads'), updated=Max('updated')).order_by('-updated', '-test_id')
    page['tests_page'] = paginate(listing_rows, request) if request is not None else Paginator(listing_rows, PAGE_SIZE).get_page(1)
    shown = list(page['tests_page'])
    tests = {t.id: t for t in Test.objects.filter(id__in=[r['test_id'] for r in shown]).select_related('dev', 'base')}
    failed = defaultdict(int)
    for count in Step.objects.filter(result__machine_id__in=ids, result__test_id__in=list(tests), state='failed')             .values('result__test_id').annotate(n=Count('id')):
        failed[count['result__test_id']] = count['n']
    page['recent'] = [{'test': tests[r['test_id']], 'label': test_label(tests[r['test_id']]), 'match': match_id(r['test_id']),
                       'games': r['games'], 'crashes': r['crashes'], 'timeloss': r['timeloss'], 'workloads': r['workloads'],
                       'updated': r['updated'], 'failures': failed[r['test_id']]} for r in shown if r['test_id'] in tests]
    page['pills'] = cpuflags.pills(info['flags'])

    # trouble now: a failed step the worker itself is blamed for (or unclear) on a test that has not ended
    trouble = []
    for step in Step.objects.filter(result__machine_id__in=ids, result__test__finished=False, state='failed',
                                    blame__in=('worker', 'unclear')).select_related('result', 'result__test', 'result__test__dev', 'result__test__base'):
        trouble.append({'step': step, 'where': '%s %s' % (step.step, step.side) if step.side else step.step,
                        'match': match_id(step.result.test_id), 'label': test_label(step.result.test)})
    page['trouble'] = trouble
    page['sessions'] = len(ids)

    # the graph, the figures that come from it, and the sections the mock has
    page['graph'] = graph(hours.last_hours(hours.box_of(machine), now), now, up=row is not None and row['state'] != 'quiet')
    page['hardware'] = hardware(info)
    page['software'] = software(info, machine)
    page['toolchains'] = toolchains(machine)
    page['approval'] = approval(machine)
    return page
