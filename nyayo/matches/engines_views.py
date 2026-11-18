"""The engine catalogue's pages: the list (open to read), editing a row and merging two (behind `action_required`)."""

import json
import re
from urllib.parse import urlparse

from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from nyayo.actions.access import action_required, may_act

from . import engines
from .models import Engine

GITHUB = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/?$")


def rows():
    return engines.catalogue().annotate(
        played=Count("matches_as_opponent", filter=Q(matches_as_opponent__match__kind="gauntlet"), distinct=True))


def listing(request):
    return render(request, "matches/engines.html", {"here": "opponents", "engines": rows(), "may_edit": may_act(request.user)})


def http_url(text):
    parts = urlparse(text)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def read_form(post):
    """The fields of the edit form as the row keeps them, and what is wrong with them."""
    problems, values = [], {}
    url = post.get("url", "").strip()
    if url and not GITHUB.match(url):
        problems.append("The repository must be a GitHub URL, https://github.com/owner/name.")
    values["url"] = url.rstrip("/")
    for name in ("ref", "asset", "binary"):
        values[name] = post.get(name, "").strip()
    text = post.get("options", "").strip()
    try:
        options = json.loads(text) if text else {}
        if not isinstance(options, dict):
            raise ValueError
    except ValueError:
        problems.append("The default options must be a JSON object, like {\"Hash\": 64}.")
        options = {}
    values["options"] = options
    value = post.get("rating_value", "").strip()
    if value:
        try:
            rating = {"value": float(value) if "." in value else int(value)}
        except ValueError:
            problems.append("The rating must be a number.")
            rating = None
        else:
            for key, field in (("list", "rating_list"), ("as_of", "rating_as_of"), ("source", "rating_source")):
                if post.get(field, "").strip():
                    rating[key] = post[field].strip()
            if rating.get("source") and not http_url(rating["source"]):
                problems.append("The rating's source must be a URL.")
        values["rating"] = rating
    else:
        values["rating"] = None
    values["aliases"] = engines.parse_aliases(post.get("aliases"))
    return values, problems


@action_required
def edit(request, pk):
    engine = get_object_or_404(Engine, pk=pk)
    form = None
    if request.method == "POST":
        values, problems = read_form(request.POST)
        if not problems:
            for name, value in values.items():
                setattr(engine, name, value)
            engine.save(update_fields=list(values))
            messages.success(request, "Saved %s." % engine)
            return redirect("opponents")
        for problem in problems:
            messages.error(request, problem)
        form = request.POST
    return render(request, "matches/engine_edit.html", {
        "here": "opponents", "engine": engine, "form": form, "options_text": json.dumps(engine.options) if engine.options else "",
        "aliases_text": "\n".join(engine.aliases), "others": engines.catalogue().exclude(pk=engine.pk)})


@action_required
def merge(request, pk):
    """GET with `other`: the confirm page. POST: do it, for good. `pk` is the row kept."""
    keep = get_object_or_404(Engine, pk=pk)
    source = request.POST if request.method == "POST" else request.GET
    other = Engine.objects.filter(pk=source.get("other") or 0).exclude(pk=keep.pk).first()
    if other is None:
        raise Http404("no other engine chosen")
    if request.method == "POST":
        try:
            engines.merge(keep, other)
        except engines.MergeError as error:
            messages.error(request, str(error))
            return redirect("opponent_edit", pk=keep.pk)
        messages.success(request, "Merged %s into %s; its name is now an alias." % (other, keep))
        return redirect("opponents")
    return render(request, "matches/engine_merge.html", {
        "here": "opponents", "keep": keep, "other": other,
        "uses": {"keep": uses(keep), "other": uses(other)}})


def uses(engine):
    return {"subject": engine.matches_as_subject.count(), "opponent": engine.matches_as_opponent.count()}
