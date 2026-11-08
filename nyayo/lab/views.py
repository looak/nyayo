"""The pages that act on tests: create one, look a branch up as it is typed, stop, restart or approve it.

Behind nyayo/actions/access.py's gate. Routed from nyayo/actions/urls.py, where the stub for Create test was.
"""

import json
import re

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from nyayo.actions.access import action_required

from . import create_test as creating, github, modify_test, tunes, utils
from .models import Book, EngineConfig, SPSARun, Test


@action_required
def create_test_page(request):
    engines = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    posted = request.POST.get('engine') if request.method == 'POST' else None
    config = next((c for c in engines if c.name == posted), engines[0] if engines else None)
    if config is None:
        return render(request, 'lab/create_test.html', {'nothing': True})

    values = creating.default_values(config)
    errors, behind, offline = {}, None, False
    if request.method == 'POST':
        values.update({key: request.POST[key].strip() for key in values if key in request.POST})
        if values['test_mode'] == creating.TUNE:
            values['spsa_frozen'] = request.POST.getlist('spsa_frozen')
            values.update({key: request.POST[key].strip() for key in request.POST if key.startswith('spsa_frozen_value_')})
        test, errors, behind = creating.create_test(values, request.user, anyway='anyway' in request.POST)
        if test:
            modify_test.report(test)
            return redirect('match', match_id=test.match_id())

    kinds = {key: {**kind, 'bounds': kind['bounds'] or creating.preset_values(config, 'default').get('bounds', '[0.00, 5.00]')}
             for key, kind in creating.KINDS.items()}
    context = {
        'values': values, 'errors': errors, 'behind': behind, 'engines': creating.engine_table(),
        'engine_names': [(c.name, c.name) for c in engines], 'presets': creating.engine_table()[config.name]['presets'],
        'kinds': kinds, 'books': [(b.name, b.name) for b in Book.objects.filter(enabled=True).order_by('name')],
        'networks': [('', 'None')] + [(n['sha256'], n['name']) for n in creating.engine_table()[config.name]['networks']],
        'syzygy': [(x, x) for x in ['DISABLED', 'OPTIONAL', '7-MAN', '6-MAN', '5-MAN', '4-MAN', '3-MAN']],
        'pgns': [(x, x) for x in creating.PGNS], 'scale': [(x, x) for x in Test.ScaleMethod.values],
        'modes': [(x, x) for x in creating.MODES], 'allowed': creating.allowed_repos(),
        'here': 'create_test',
    }
    return render(request, 'lab/create_test.html', context, status=400 if errors else 200)


TUNE_SHARED = ('branch', 'bench', 'network', 'repo', 'tc', 'options')  # a tune plays one commit against itself: base is dev


def tune_values(config, post):
    """The create-tune form's values from what was posted: the engine's defaults, the chosen preset's fields, and base the same
    as dev. Frozen parameters come as `spsa_frozen` names with `spsa_frozen_value_<name>`, or, without script, as the lines
    `Name = value` of `spsa_frozen_text`."""
    values = creating.default_values(config)
    values.update({key: post[key].strip() for key in values if key in post})
    preset = values.get('preset')
    if preset in creating.preset_names(config):
        values.update(creating.preset_values(config, preset))
        values.update({key: post[key].strip() for key in ('name', 'priority', 'book', 'dev_branch') if key in post})
    for name in TUNE_SHARED:
        values['base_' + name] = values['dev_' + name]
    values['test_mode'] = creating.TUNE
    values['spsa_frozen'] = [name for name in post.getlist('spsa_frozen') if name]
    values.update({key: post[key].strip() for key in post if key.startswith('spsa_frozen_value_')})
    for line in post.get('spsa_frozen_text', '').splitlines():
        name, _, value = (part.strip() for part in line.partition('='))
        if name and name not in values['spsa_frozen']:
            values['spsa_frozen'].append(name)
            values['spsa_frozen_value_' + name] = value
    values['spsa_then_sprt'] = '1' if post.get('spsa_then_sprt') else ''
    return values


@action_required
def create_tune_page(request):
    engines = list(EngineConfig.objects.filter(enabled=True).order_by('name'))
    posted = request.POST.get('engine') if request.method == 'POST' else None
    config = next((c for c in engines if c.name == posted), engines[0] if engines else None)
    if config is None:
        return render(request, 'lab/create_tune.html', {'nothing': True, 'here': 'create_tune'})

    errors = {}
    if request.method == 'POST':
        values = tune_values(config, request.POST)
        test, errors, _ = creating.create_tune(values, request.user, anyway=True)
        if test:
            modify_test.report(test)
            return redirect('match', match_id=test.match_id())
    else:
        values = creating.default_values(config)
        values.update(spsa_frozen=[], spsa_inputs=request.GET.get('inputs', ''))

    table = creating.engine_table()
    context = {
        'values': values, 'errors': errors, 'engine_names': [(c.name, c.name) for c in engines],
        'presets': table[config.name]['presets'], 'books': [(b.name, b.name) for b in Book.objects.filter(enabled=True).order_by('name')],
        'rules': [(x, x) for x in SPSARun.UpdateRule.values], 'engines': table,
        'frozen_json': json.dumps({name: values.get('spsa_frozen_value_' + name, '') for name in values.get('spsa_frozen', [])}),
        'frozen_text': chr(10).join('%s = %s' % (name, values.get('spsa_frozen_value_' + name, '')) for name in values.get('spsa_frozen', [])),
        'here': 'create_tune',
    }
    return render(request, 'lab/create_tune.html', context, status=400 if errors else 200)


def facts(repo, ref):
    """What the page's "worked out for you" shows of one ref: found, or why not."""
    ref = ref.strip()
    if not ref:
        return {}
    try:
        found = github.look_up(repo, ref)
    except github.GithubDown:
        return {'down': True}
    if found is None:
        return {'missing': True}
    out = {'sha': found['sha'], 'short': found['sha'][:12], 'subject': found['message'].strip().partition('\n')[0],
           'bench': utils.determine_bench('', found['message']), 'name': ref, 'named_by': ''}
    if github.is_sha(ref):
        branch = github.branch_of_sha(repo, ref)
        out['name'], out['named_by'] = (branch, 'the branch whose head it is') if branch else (out['subject'], "the commit's first line, as no branch has it")
    return out


def allowed_source(repo):
    return bool(re.match(r'^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/?$', repo)) and creating.repo_path(repo) in creating.allowed_repos()


@action_required
def lookup(request):
    """GET: the page's script asks what GitHub says of the typed branches. Never a failure: a problem is in the answer."""
    repo = request.GET.get('repo', '')
    if not allowed_source(repo):
        return JsonResponse({'error': "That source isn't the source of an engine."})
    dev, base = facts(repo, request.GET.get('dev', '')), facts(repo, request.GET.get('base', ''))
    behind = github.commits_behind(repo, dev['sha'], base['sha']) if dev.get('sha') and base.get('sha') else None
    return JsonResponse({'dev': dev, 'base': base, 'behind': behind})


@action_required
def branches(request):
    """GET: the branch names of a source, for the page's suggestions, the engine's base branch first. Never a failure: a
    problem is an empty list and the reason."""
    repo = request.GET.get('repo', '')
    if not allowed_source(repo):
        return JsonResponse({'branches': [], 'error': "That source isn't the source of an engine."})
    try:
        names = sorted(set(github.branches(repo)))
    except github.GithubDown:
        return JsonResponse({'branches': [], 'error': 'GitHub did not answer.'})
    names.sort(key=lambda name: name != creating.DEFAULT_BASE)
    return JsonResponse({'branches': names})


@action_required
@require_POST
def modify(request, test_id, action):
    if action not in modify_test.ACTIONS:
        raise Http404
    test = get_object_or_404(Test, pk=test_id)
    if not modify_test.may(request.user, action):
        raise PermissionDenied
    if action == 'unapprove' and action not in modify_test.offers(test):
        return HttpResponse('Only a test with no games played, and still running, can be unapproved: stop it instead.', status=409, content_type='text/plain')
    modify_test.apply(request.user, test, action)
    modify_test.report(test)
    return redirect('match', match_id=test.match_id())


def tune_of(test_id):
    test = get_object_or_404(Test, pk=test_id, test_mode='SPSA')
    return test


@action_required
@require_POST
def tune_sprt(request, test_id):
    """POST: queue an SPRT of the tune's averaged values against its start now, the page's "SPRT these values now"."""
    test = tune_of(test_id)
    queued = tunes.queue_sprt(test, user=request.user)
    return redirect('match', match_id=queued.match_id())


def tune_history_json(request, test_id):
    """GET: the tune's snapshot rows, with the checks' columns. Open, as the live pages are."""
    test = tune_of(test_id)
    return JsonResponse({'tune': test.match_id(), 'history': tunes.history(test.spsa_run)})


def tune_history_csv(request, test_id):
    """GET: the same rows, one column a parameter."""
    test = tune_of(test_id)
    response = HttpResponse(tunes.history_csv(test.spsa_run), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="%s-history.csv"' % test.match_id()
    return response
