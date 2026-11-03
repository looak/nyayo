
# Adapted for nyayo from OpenBench's OpenBench/workloads/create_workload.py and verify_workload.py (openbench_eg
# da629d7), for a test (an SPRT or a fixed number of games). Tunes and datagens are step 7's. The checks and their
# messages are OpenBench's; what changed is that the values come as a dict of the form's strings, the errors are by
# field so the page can mark it, the author is a Django user, GitHub is asked through github.py (down is not the same as
# no), a repository must be one an enabled engine is built from, and a branch behind its base stops the form until
# it is sent again with "Create it anyway".

import math
import os
import re

from django.db import transaction

from . import github, utils
from .models import Book, Engine, EngineConfig, LogEvent, Network, Test

SYZYGY = ['OPTIONAL', 'DISABLED', '3-MAN', '4-MAN', '5-MAN', '6-MAN', '7-MAN']
PGNS   = ['COMPACT', 'VERBOSE']
MODES  = ['SPRT', 'GAMES']

# The two things a test can ask, as the page's pills: the engine's own bounds, or a regression bound
KINDS = {
    'gain'       : { 'label' : 'Gain',           'hint' : 'is it stronger', 'bounds' : None },
    'regression' : { 'label' : 'Non-regression', 'hint' : 'is it no worse', 'bounds' : '[-3.00, 1.00]' },
}

# An engine's presets name OpenBench's fields; the form's are shorter
ALIASES = {
    'book_name' : 'book', 'test_bounds' : 'bounds', 'test_confidence' : 'confidence', 'test_max_games' : 'max_games',
    'dev_time_control' : 'dev_tc', 'base_time_control' : 'base_tc',
}

def preset_values(config, name):

    # The engine's defaults with a preset on top, as the form's fields. both_* is dev_* and base_*
    table  = (config.presets or {}).get('test_presets', {})
    merged = { **table.get('default', {}), **table.get(name, {}) }
    out    = {}

    for key, value in merged.items():
        for field in (['dev_' + key[5:], 'base_' + key[5:]] if key.startswith('both_') else [key]):
            out[ALIASES.get(field, field)] = str(value)

    return out

def preset_names(config):
    return [name for name in (config.presets or {}).get('test_presets', {}) if name != 'default']

def engine_table():

    # What the page's script needs of each enabled engine, as JSON
    table = {}
    for config in EngineConfig.objects.filter(enabled=True).order_by('name'):
        names = preset_names(config)
        table[config.name] = {
            'source'  : config.source,
            'nps'     : config.nps,
            'private' : config.private,
            'default' : preset_values(config, 'default'),
            'presets' : { name : preset_values(config, name) for name in names },
            'networks': [{ 'name' : n.name, 'sha256' : n.sha256, 'default' : n.default } for n in Network.objects.filter(engine=config.name)],
        }
    return table

def default_values(config):

    # What the form starts with: the engine, its defaults, its first preset
    names  = preset_names(config)
    values = {
        'engine' : config.name, 'dev_branch' : '', 'base_branch' : 'main', 'name' : '', 'test_kind' : 'gain',
        'preset' : names[0] if names else '', 'dev_repo' : config.source, 'base_repo' : config.source,
        'dev_bench' : '', 'base_bench' : '', 'dev_network' : '', 'base_network' : '',
        'dev_options' : 'Threads=1 Hash=8', 'base_options' : 'Threads=1 Hash=8', 'dev_tc' : '8.0+0.08', 'base_tc' : '8.0+0.08',
        'test_mode' : 'SPRT', 'bounds' : '[0.00, 5.00]', 'confidence' : '[0.05, 0.05]', 'max_games' : '',
        'book' : '', 'upload_pgns' : 'COMPACT', 'priority' : '0', 'throughput' : '1000', 'workload_size' : '32',
        'syzygy_wdl' : 'DISABLED', 'syzygy_adj' : 'OPTIONAL', 'win_adj' : 'movecount=3 score=400',
        'draw_adj' : 'movenumber=40 movecount=8 score=10', 'scale_method' : 'BASE', 'scale_nps' : str(config.nps),
        'info' : '',
    }
    values.update(preset_values(config, names[0] if names else 'default'))
    if not values['book']:
        book = Book.objects.filter(enabled=True).order_by('name').first()
        values['book'] = book.name if book else ''
    return values

def repo_path(repo):
    return repo.strip().removeprefix('https://github.com/').rstrip('/')

def allowed_repos():
    return sorted({ repo_path(c.source) for c in EngineConfig.objects.filter(enabled=True) })

def verify(values, errors):

    # OpenBench's verify_test_creation, by field. Nothing here asks GitHub.
    def need(field, ok, message):
        if field not in errors and not ok:
            errors[field] = message

    def integer(field, name):
        try: int(values[field]); return True
        except: errors.setdefault(field, '"%s" is not an Integer' % name); return False

    config = EngineConfig.objects.filter(name=values['engine'], enabled=True).first()
    need('engine', config, '%s was not found in the configuration' % (values['engine'] or 'Engine'))

    for side in ('dev', 'base'):
        repo = values[side + '_repo'].strip()
        need(side + '_repo', re.match(r'^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/?$', repo),
             'Sources must be found on https://github.com/<User>/<Repo>')
        need(side + '_repo', repo_path(repo) in allowed_repos(),
             '%s isn\'t the source of an engine, so its code won\'t run on a worker. Add the engine under Engines.' % (repo_path(repo) or 'That repository'))

        network = values[side + '_network']
        need(side + '_network', network == '' or Network.objects.filter(engine=values['engine'], sha256=network).exists(),
             'Unknown Network Provided for %s' % side.capitalize())

        for option in ('Threads', 'Hash'):
            try: assert int(utils.extract_option(values[side + '_options'], option)) >= 1
            except: errors.setdefault(side + '_options', '"%s" needs to be at least 1 for %s Options' % (option, side.capitalize()))

        try: utils.TimeControl.parse(values[side + '_tc'])
        except: errors.setdefault(side + '_tc', '%s Time Control is not parsable' % side.capitalize())

    need('book', Book.objects.filter(name=values['book'], enabled=True).exists(), 'Book was not found in the configuration')
    need('upload_pgns', values['upload_pgns'] in PGNS + ['FALSE'], '"Upload PGNs" must be COMPACT or VERBOSE')
    need('test_mode', values['test_mode'] in MODES, 'Unknown Test Mode')

    if values['test_mode'] == 'SPRT':
        pattern = r'^\[(-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\]$'
        try:
            match = re.match(pattern, values['bounds'])
            assert float(match.group(1)) < float(match.group(2))
        except: errors.setdefault('bounds', 'SPRT Bounds must be formatted as [float1, float2]')
        try:
            match = re.match(pattern, values['confidence'])
            assert 0.00 < float(match.group(1)) < 1.00
            assert 0.00 < float(match.group(2)) < 1.00
        except: errors.setdefault('confidence', 'Confidence Bounds must be formatted as [float1, float2], within (0.00, 1.00)')

    if values['test_mode'] == 'GAMES':
        try: assert int(values['max_games']) > 0
        except: errors.setdefault('max_games', 'Fixed Games Tests must last at least one game')

    integer('priority', 'Priority')
    try: assert float(values['throughput']) > 0
    except: errors.setdefault('throughput', '"Throughput" is not greater than 0')
    try: assert int(values['workload_size']) > 0
    except: errors.setdefault('workload_size', '"Workload Size" is not greater than 0')
    try: assert int(values['scale_nps']) > 0
    except: errors.setdefault('scale_nps', '"Scale NPS" is not greater than 0')

    for field, name in (('syzygy_wdl', 'Syzygy WDL'), ('syzygy_adj', 'Syzygy Adjudication')):
        need(field, values[field] in SYZYGY, '%s must be in %s' % (name, ', '.join(SYZYGY)))

    need('scale_method', values['scale_method'] in Test.ScaleMethod.values,
         'Unknown Scale Method. Expected one of {%s}.' % ', '.join(Test.ScaleMethod.values))

    need('win_adj', values['win_adj'] == 'None' or re.match('movecount=[0-9]+ score=[0-9]+', values['win_adj']),
         'Invalid Win Adjudication Setting. Try "None"?')
    need('draw_adj', values['draw_adj'] == 'None' or re.match('movenumber=[0-9]+ movecount=[0-9]+ score=[0-9]+', values['draw_adj']),
         'Invalid Draw Adjudication Setting. Try "None"?')

    return config

def collect_github_info(values, side, config, errors):

    # The commit a side names, from GitHub: (source, branch, sha, bench, message, title), as a dict. When GitHub
    # doesn't answer, a full sha and a bench typed in are enough, and info['offline'] says so.
    field  = side + '_branch'
    branch = values[field].strip()
    repo   = values[side + '_repo'].strip()

    if not branch:
        errors.setdefault(field, 'Pick a %s branch.' % side)
        return None

    if config.private and not os.environ.get(github.TOKEN_VARIABLE):
        errors.setdefault(field, 'The server has no access token for this engine')
        return None

    bysha   = github.is_sha(branch)
    typed   = values[side + '_bench'].strip()
    offline = False

    try:
        found = github.look_up(repo, branch)
    except github.GithubDown:
        found, offline = None, True

    if offline:
        if bysha and utils.determine_bench(typed, ''):
            sha = branch.lower()
            return { 'source' : utils.path_join(github.api_base(repo), 'zipball', sha), 'branch' : branch, 'sha' : sha,
                     'bench' : utils.determine_bench(typed, ''), 'message' : '', 'title' : '', 'offline' : True }
        errors.setdefault(field, 'GitHub did not answer, so %s could not be looked up. To go without it, type the full 40 character sha and the bench under Advanced.' % branch)
        return None

    if found is None:
        errors.setdefault(field, '%s could not be found in %s.' % (branch, repo_path(repo)))
        return None

    # Extract the bench from the web form, or from the commit message
    if not (bench := utils.determine_bench(typed, found['message'])):
        errors.setdefault(field, 'Unable to parse a Bench for %s: its commit message has no Bench: line. Add one and push, or type the number under Advanced.' % branch)
        return None

    # A sha is named by the branch it is the head of, or the pull request it is in, as if that was typed. With
    # neither, the branch stays the sha, and the commit's first line is what the test is called
    title = ''
    if bysha:
        if name := github.branch_of_sha(repo, branch):
            branch = name
        else:
            title = found['message'].strip().partition('\n')[0].strip()

    source = utils.path_join(github.api_base(repo), 'zipball', found['sha'])
    return { 'source' : source, 'branch' : branch, 'sha' : found['sha'], 'bench' : bench, 'message' : found['message'],
             'title' : title, 'offline' : False }

def create_test(values, user, anyway=False):

    # (test, errors, behind). A test only when there are no errors and the branch isn't behind its base, or it was
    # sent again anyway. behind is { dev, base, commits } for the page's warning.
    errors = {}
    config = verify(values, errors)
    dev = base = None

    if config and not errors.get('engine'):
        dev  = collect_github_info(values, 'dev', config, errors)
        base = collect_github_info(values, 'base', config, errors)

    if errors:
        return None, errors, None

    # Cannot compare across engines; the same repository is the only one a test can have here
    if not anyway and values['dev_repo'].strip() == values['base_repo'].strip() and not (dev['offline'] or base['offline']):
        commits = github.commits_behind(values['dev_repo'], dev['sha'], base['sha'])
        if commits:
            return None, {}, { 'dev' : dev['branch'], 'base' : base['branch'], 'commits' : commits }

    with transaction.atomic():
        test                   = Test()
        test.author            = user
        test.book_name         = values['book']
        test.upload_pgns       = utils.always_upload(values['upload_pgns'])
        test.info              = utils.determine_info(values['info'].strip(), dev['message'], values['engine'], values['engine'],
                                                      values['dev_branch'], values['base_branch'])
        test.name              = (values['name'].strip() or dev['title'])[:200].strip()

        test.dev               = Engine.objects.create(name=dev['branch'], source=dev['source'], sha=dev['sha'], bench=dev['bench'])
        test.dev_repo          = values['dev_repo'].strip()
        test.dev_engine        = values['engine']
        test.dev_options       = values['dev_options']
        test.dev_network       = values['dev_network']
        test.dev_time_control  = utils.TimeControl.parse(values['dev_tc'])

        test.base              = Engine.objects.create(name=base['branch'], source=base['source'], sha=base['sha'], bench=base['bench'])
        test.base_repo         = values['base_repo'].strip()
        test.base_engine       = values['engine']
        test.base_options      = values['base_options']
        test.base_network      = values['base_network']
        test.base_time_control = utils.TimeControl.parse(values['base_tc'])

        test.workload_size     = int(values['workload_size'])
        test.priority          = int(values['priority'])
        test.throughput        = int(values['throughput'])

        test.syzygy_wdl        = values['syzygy_wdl']
        test.syzygy_adj        = values['syzygy_adj']
        test.win_adj           = values['win_adj']
        test.draw_adj          = values['draw_adj']

        test.scale_method      = values['scale_method']
        test.scale_nps         = int(values['scale_nps'])

        test.test_mode         = values['test_mode']

        if test.test_mode == 'SPRT':
            test.elolower = float(values['bounds'].split(',')[0].lstrip('['))
            test.eloupper = float(values['bounds'].split(',')[1].rstrip(']'))
            test.alpha    = float(values['confidence'].split(',')[1].rstrip(']'))
            test.beta     = float(values['confidence'].split(',')[0].lstrip('['))
            test.lowerllr = math.log(test.beta / (1.0 - test.alpha))
            test.upperllr = math.log((1.0 - test.beta) / test.alpha)

        if test.test_mode == 'GAMES':
            test.max_games = int(values['max_games'])

        if test.dev_network:
            test.dev_netname = Network.objects.get(engine=test.dev_engine, sha256=test.dev_network).name

        if test.base_network:
            test.base_netname = Network.objects.get(engine=test.base_engine, sha256=test.base_network).name

        # The people who may sign in here are the approvers; a test made by a user who isn't the owner waits to be approved
        test.approved = user.is_superuser
        test.save()

        LogEvent.objects.create(author=user.get_username(), summary='CREATE P=%d TP=%d' % (test.priority, test.throughput),
                                log_file='', test_id=test.id)

    return test, {}, None
