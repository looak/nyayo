"""The pages that take matches away and bring them back: the confirm page, the doing of it, the list of removed matches.
Every one is behind `action_required`; reading is open, acting is not."""

from django.contrib import messages
from django.db.models import Count, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from nyayo.actions.access import action_required

from . import removal
from .models import Batch, Game, Match, Upload

MODES = ("remove", "delete")


def chosen(request):
    """The matches named by `id` in the query or the form, in the order given, without repeats; missing ids are dropped."""
    source = request.POST if request.method == "POST" else request.GET
    ids = list(dict.fromkeys(i for i in source.getlist("id") if i))
    found = Match.objects.in_bulk(ids)
    return [found[i] for i in ids if i in found]


def holds(match):
    """What deleting this match would take, for the confirm page."""
    from nyayo.lab import modify_test

    results = match.results.aggregate(games=Sum("games"))["games"] or 0
    kept = Batch.objects.filter(match=match).aggregate(n=Count("pk"), games=Sum("games"))
    test = modify_test.test_of(match)
    return {"match": match, "name": removal.name_of(match), "played": results, "game_rows": Game.objects.filter(match=match).count(),
            "batches": kept["n"], "batch_games": kept["games"] or 0, "uploads": Upload.objects.filter(match=match).count(),
            "test": test, "running": match.state == "running" or (test is not None and not test.finished)}


def confirm(request):
    matches = chosen(request)
    if not matches:
        raise Http404("no match chosen")
    mode = request.GET.get("mode") if request.GET.get("mode") in MODES else "remove"
    return render(request, "matches/remove_confirm.html", {
        "rows": [holds(m) for m in matches], "mode": mode, "blockers": removal.blockers(matches),
        "many": len(matches) > 1, "total_games": sum(holds(m)["played"] for m in matches)})


@action_required
def remove(request):
    """GET: the confirm page. POST: do it."""
    return do(request) if request.method == "POST" else confirm(request)


def do(request):
    matches = chosen(request)
    mode = request.POST.get("mode")
    if not matches or mode not in MODES:
        raise Http404("nothing chosen")
    here = redirect("matches")
    if removal.blockers(matches):
        for sentence in removal.blockers(matches):
            messages.error(request, sentence)
        return here
    if mode == "delete":
        done = removal.delete(matches, request.user)
        messages.success(request, "Deleted %d match%s with %s. They are gone for good." % (
            len(done), "" if len(done) == 1 else "es", "its games" if len(done) == 1 else "their games"))
    else:
        done = removal.remove(matches, request.user)
        messages.success(request, "Removed %d match%s; the games stay in Games and the exports. They can be brought back." % (
            len(done), "" if len(done) == 1 else "es"))
        if len(done) and request.user.is_authenticated:
            return redirect("matches_removed")
    return here


@action_required
def removed(request):
    """The matches taken out of the lists, with *Bring it back*."""
    matches = Match.objects.filter(hidden=True).select_related("subject").order_by("-updated_at")
    return render(request, "matches/removed.html", {"matches": [holds(m) for m in matches]})


@action_required
@require_POST
def restore(request):
    matches = [m for m in chosen(request) if m.hidden]
    if not matches:
        raise Http404("nothing to bring back")
    removal.bring_back(matches)
    messages.success(request, "Brought back %s." % ", ".join(removal.name_of(m) for m in matches))
    return redirect("matches_removed")
