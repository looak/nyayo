"""Reading an engine's parameters: the JSON the create-tune page polls, and the engine's page with its tunables.

Behind nyayo/actions/access.py's gate, as every page that starts work. A page asks for a branch or a commit of an engine; the
commit is looked up on GitHub, and its list is the cache's (`lab/tunables.py`) or a worker's to read.
"""

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from nyayo.actions.access import action_required

from . import create_test as creating, github, tunables
from .models import EngineConfig, TunableList


def engine_of(name):
    return get_object_or_404(EngineConfig, name=name, enabled=True)


def resolve(config, ref):
    """({sha, short, subject, branch}, None) for what is typed, or (None, what is wrong). An empty ref is the engine's base branch."""
    ref = (ref or '').strip() or creating.DEFAULT_BASE
    try:
        found = github.look_up(config.source, ref)
    except github.GithubDown:
        return None, 'GitHub did not answer, so %s could not be looked up.' % ref
    if found is None:
        return None, 'There is no branch or commit %s in %s.' % (ref, creating.repo_path(config.source))
    return {'sha': found['sha'], 'short': found['sha'][:7], 'subject': found['message'].strip().partition('\n')[0], 'branch': ref}, None


def answer(config, found, row, queued=False):
    return {**tunables.facts(row), 'sha': found['sha'], 'short': found['short'], 'subject': found['subject'], 'branch': found['branch'],
            'engine': config.name, 'make_args': config.tune_make_args, 'queued': queued}


@action_required
def state(request):
    """GET ?engine=&ref= (or &sha=): where the list of that commit is. Nothing is queued."""
    config = engine_of(request.GET.get('engine', ''))
    if sha := request.GET.get('sha', '').strip():
        found = {'sha': sha, 'short': sha[:7], 'subject': '', 'branch': request.GET.get('ref', '')}
    else:
        found, problem = resolve(config, request.GET.get('ref', ''))
        if found is None:
            return JsonResponse({'error': problem, 'state': 'none'})
    return JsonResponse(answer(config, found, tunables.of(config, found['sha'])))


@action_required
@require_POST
def read(request):
    """POST engine, ref (or sha), retry: queue a read of that commit's list, once per commit; a failed one only when `retry`."""
    config = engine_of(request.POST.get('engine', ''))
    if sha := request.POST.get('sha', '').strip():
        found = {'sha': sha, 'short': sha[:7], 'subject': '', 'branch': request.POST.get('ref', '')}
    else:
        found, problem = resolve(config, request.POST.get('ref', ''))
        if found is None:
            return JsonResponse({'error': problem, 'state': 'none'})
    row, queued = tunables.request(config, found['sha'], found['branch'], retry=bool(request.POST.get('retry')))
    return JsonResponse(answer(config, found, row, queued))


@action_required
def engines_page(request):
    configs = EngineConfig.objects.order_by('name')
    rows = [{'config': c, 'done': TunableList.objects.filter(engine=c.name, state='done').count()} for c in configs]
    return render(request, 'lab/engines.html', {'rows': rows, 'here': 'engines'})


@action_required
def engine_page(request, name):
    """One engine: what its tune build exposes at a branch, from the cache, with Refresh."""
    config = get_object_or_404(EngineConfig, name=name)
    branch = request.GET.get('branch', '').strip() or creating.DEFAULT_BASE
    found, problem = resolve(config, branch)
    row = tunables.of(config, found['sha']) if found else None
    words = ' '.join(config.tune_make_args.split())
    shown, older = row, None
    if row is None or row.state != TunableList.State.DONE:
        # nothing read of this commit yet: the newest list that was read is shown, and said to be of another commit
        older = TunableList.objects.filter(engine=config.name, make_args=words, state='done').order_by('-read').first()
    context = {
        'config': config, 'branch': branch, 'found': found, 'problem': problem, 'row': shown, 'older': older,
        'params': tunables.parse(shown) if shown is not None and shown.state == 'done' else [],
        'older_params': tunables.parse(older) if older is not None else [],
        'history': TunableList.objects.filter(engine=config.name, state='done').order_by('-read')[:10],
        'here': 'engines', 'waiting': row is not None and row.state in ('queued', 'reading'),
    }
    return render(request, 'lab/engine.html', context)


@action_required
@require_POST
def engine_refresh(request, name):
    """POST branch: resolve it to its newest commit and read that unless it has been; a failed read is read again."""
    config = get_object_or_404(EngineConfig, name=name)
    branch = request.POST.get('branch', '').strip() or creating.DEFAULT_BASE
    found, problem = resolve(config, branch)
    if found:
        tunables.request(config, found['sha'], found['branch'], retry=True)
    return redirect('%s?branch=%s' % (reverse('engine', args=[config.name]), branch))
