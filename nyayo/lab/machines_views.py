"""The Machines pages, open to read: the list of workers and the page of one. What they show is nyayo/lab/machines.py's.
What changes a worker (approve, turn away, enable, disable) or makes a join code is behind the gate, and every change of a
worker is an event (registration.log)."""

import datetime

from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from nyayo.actions.access import action_required, may_act

from . import client_views, machines, registration
from .models import JoinCode, Machine, Place


def list_context(request, now, **extra):
    rows = machines.listing(now)
    workers = [r for r in rows if r['worker']]
    heard = [r for r in workers if r['state'] in ('playing', 'idle')]
    return {
        'here': 'machines', 'rows': rows, 'now': now, 'may_act': may_act(request.user), 'server': client_views.server_address(request),
        'playing': sum(1 for r in workers if r['state'] == 'playing'), 'workers': len(workers),
        'threads': sum(r['threads'] for r in heard if isinstance(r['threads'], int)),
        'tokens': sum(1 for r in rows if r['token'] and r['token']['active']),
        'revoked': sum(1 for r in rows if r['token'] and not r['token']['active']),
        'quiet': sum(1 for r in workers if r['state'] == 'quiet'),
        'pending': sum(1 for r in workers if r['state'] == 'pending'),
        'codes': list(JoinCode.objects.filter(used_by__isnull=True, expires__gt=now).order_by('expires')) if may_act(request.user) else [],
        **extra,
    }


def machines_page(request):
    now = timezone.now()
    registration.purge_pending(now)
    return render(request, 'lab/machines.html', list_context(request, now))


def machine_page(request, name):
    page = machines.find(name, request=request)
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


@action_required
@require_POST
def machine_action(request):
    """Approve, turn away, enable or disable a worker. Back to the page it came from."""
    machine = Machine.objects.filter(pk=request.POST.get('id') or 0).first() if str(request.POST.get('id', '')).isdigit() else None
    if machine is None:
        raise Http404
    try:
        registration.act(machine, request.POST.get('action', ''), request.user.get_username())
    except ValueError as error:
        return HttpResponseBadRequest(str(error), content_type='text/plain')
    return redirect('machine', name=machine.name) if request.POST.get('back') == 'machine' else redirect('machines')


@action_required
@require_POST
def machine_join_code(request):
    """Makes a join code and shows it on the list, once: only its hash is kept."""
    try:
        hours = int(request.POST.get('hours') or registration.CODE_HOURS_DEFAULT)
    except ValueError:
        return HttpResponseBadRequest('hours is a number', content_type='text/plain')
    code, row = registration.make_code(request.user.get_username(), hours)
    now = timezone.now()
    return render(request, 'lab/machines.html', list_context(request, now, new_code=code, new_code_until=row.expires))


@action_required
@require_POST
def machine_code_revoke(request):
    """Takes a join code that has not been used away."""
    if str(request.POST.get('id', '')).isdigit():
        JoinCode.objects.filter(pk=int(request.POST['id']), used_by__isnull=True).delete()
    return redirect('machines')
