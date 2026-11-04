"""The Machines pages, open to read: the list of boxes and the page of one. What they show is nyayo/lab/machines.py's."""

from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from nyayo.actions.access import action_required, may_act

from . import machines
from .models import Place


def machines_page(request):
    now = timezone.now()
    rows = machines.listing(now)
    workers = [r for r in rows if r['worker']]
    heard = [r for r in workers if r['state'] != 'quiet']
    return render(request, 'lab/machines.html', {
        'here': 'machines', 'rows': rows, 'now': now,
        'playing': sum(1 for r in workers if r['state'] == 'playing'), 'workers': len(workers),
        'threads': sum(r['threads'] for r in heard if isinstance(r['threads'], int)),
        'tokens': sum(1 for r in rows if r['token'] and r['token']['active']),
        'revoked': sum(1 for r in rows if r['token'] and not r['token']['active']),
        'quiet': sum(1 for r in workers if r['state'] == 'quiet'),
    })


def machine_page(request, name):
    page = machines.find(name)
    if page is None:
        raise Http404
    return render(request, 'lab/machine.html', {'here': 'machines', 'now': timezone.now(), 'may_act': may_act(request.user), **page})


@action_required
@require_POST
def machine_place(request):
    """Where a box is, as typed by someone signed in; kept by its name, lowercase. Empty takes it away."""
    name, text = request.POST.get('name', '').strip(), ' '.join(request.POST.get('text', '').split())[:100]
    page = machines.find(name) if name else None
    if not page or not page['nameable']:
        raise Http404
    if text:
        Place.objects.update_or_create(name=name.lower(), defaults={'text': text})
    else:
        Place.objects.filter(name=name.lower()).delete()
    return redirect('machine', name=name)

