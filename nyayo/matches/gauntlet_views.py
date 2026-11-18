"""The gauntlet page: pick a subject and opponents from the engine catalogue and get the `gauntlet.toml` uwanja takes.

Everything is in the query string (a GET form), so a page can be bookmarked or linked and the file is rebuilt from it;
nothing is stored. Staff only, through `action_required`.
"""

import json
import re

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render

from nyayo.actions.access import action_required

from . import gauntlet_file as gf
from .models import Engine


def with_release():
    """The rows a gauntlet file can fetch: a repository and a tag."""
    return Engine.objects.exclude(Q(url="") | Q(ref="")).order_by("name", "version")


def whole(text, label, problems, minimum=1):
    try:
        number = int(str(text).strip())
        if number < minimum:
            raise ValueError
        return number
    except ValueError:
        problems.append(f"{label} must be a whole number of at least {minimum}.")
        return None


def read_options(text, problems):
    """An engine's options typed as a JSON object of names to strings, numbers or booleans, or None for the default."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        options = json.loads(text)
        if not isinstance(options, dict) or not all(isinstance(v, (str, int, float, bool)) for v in options.values()):
            raise ValueError
        return options
    except ValueError:
        problems.append("The subject's options must be a JSON object of names to text, numbers or true/false, like {\"Hash\": 64}.")
        return None


def as_number(text):
    try:
        return float(str(text).strip())
    except ValueError:
        return None


def default_subject(rows):
    """The newest Elephant release in the catalogue, else nothing."""
    elephants = [r for r in rows if "elephant" in r.name.casefold()]
    return max(elephants, key=lambda r: r.pk) if elephants else None


@action_required
def page(request):
    get = request.GET
    submitted = bool(get)
    rows = list(with_release())
    problems = []

    # the subject: a catalogue engine, or a local command
    chosen = get.get("subject", "")
    if not submitted:
        default = default_subject(rows)
        chosen = str(default.pk) if default else "cmd"
    subject_row = next((r for r in rows if str(r.pk) == chosen), None) if chosen != "cmd" else None
    cmd = get.get("cmd", "").strip()
    subject_name = get.get("subject_name", "").strip()
    options = read_options(get.get("subject_options"), problems)
    if submitted and subject_row is None:
        if chosen != "cmd":
            problems.append("Pick the engine under test, or give a command.")
        elif not cmd:
            problems.append("The command of the engine under test is needed.")
        elif "\n" in cmd or "\r" in cmd:
            problems.append("The command is one line.")
        elif not subject_name:
            problems.append("A local engine needs a name.")

    # the settings
    radius = whole(get.get("radius", gf.DEFAULT_RADIUS), "The range", problems, 0) if get.get("radius") else gf.DEFAULT_RADIUS
    settings = {
        "name": get.get("name", "").strip() or gf.default_name(subject_row, subject_name, cmd),
        "tc": get.get("tc", gf.DEFAULT_TC).strip() or gf.DEFAULT_TC,
        "book": get.get("book", gf.DEFAULT_BOOK).strip() if submitted else gf.DEFAULT_BOOK,
        "rounds": whole(get.get("rounds", gf.DEFAULT_ROUNDS), "Rounds", problems),
        "concurrency": whole(get.get("concurrency", gf.DEFAULT_CONCURRENCY), "Concurrency", problems),
    }
    if not gf.NAME.fullmatch(settings["name"]):
        problems.append("The name may only hold letters, digits, '.', '_' and '-'.")
    if re.search(r"[\x00-\x1f\x7f]", settings["tc"] + (settings["book"] or "")):
        problems.append("The time control and the book are one line each.")

    # the opponents: by hand, by name, or by a rating range around the subject's ours
    candidates = [r for r in rows if subject_row is None or r.pk != subject_row.pk]
    picked = {str(pk) for pk in get.getlist("opp")}
    pick, notice = get.get("pick"), None
    middle = None
    if subject_row is not None and isinstance(subject_row.ours, dict):
        middle = subject_row.ours.get("value")
    if middle is None:
        middle = as_number(get.get("centre", ""))
    if pick == "range":
        if middle is None:
            problems.append("A rating range needs a centre: the engine under test has no rating of ours, so type one.")
        else:
            near = gf.within(candidates, middle, radius if radius is not None else gf.DEFAULT_RADIUS)
            picked = {str(r.pk) for r in near}
            notice = f"{len(near)} opponents within {radius} of {middle:.0f}."
    elif pick == "name":
        words = [w.strip().casefold() for w in re.split(r"[,\n]", get.get("q", "")) if w.strip()]
        found = {str(r.pk) for r in candidates if any(w in f"{r.name} {r.version}".casefold() or
                                                       any(w in a.casefold() for a in r.aliases if isinstance(a, str))
                                                       for w in words)}
        picked |= found
        notice = f"{len(found)} opponents match {get.get('q', '').strip()!r}."
    selected = [r for r in candidates if str(r.pk) in picked]
    if submitted and not selected and pick is None:
        problems.append("Pick at least one opponent.")

    text = None
    if submitted and not problems and selected:
        text = gf.build(settings, subject_row, cmd, subject_name, options, selected)
        if "download" in get:
            response = HttpResponse(text, content_type="application/toml; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{settings["name"]}.toml"'
            return response

    form = {"subject": chosen, "cmd": cmd, "subject_name": subject_name,
            "subject_options": get.get("subject_options", ""),
            "name": get.get("name", ""), "tc": settings["tc"], "book": settings["book"] or "", "rounds": get.get("rounds", gf.DEFAULT_ROUNDS),
            "concurrency": get.get("concurrency", gf.DEFAULT_CONCURRENCY), "radius": get.get("radius", gf.DEFAULT_RADIUS),
            "centre": get.get("centre", ""), "q": get.get("q", "")}
    return render(request, "matches/gauntlet.html", {
        "here": "gauntlet", "form": form, "subjects": rows, "candidates": candidates, "picked": picked,
        "problems": problems if submitted else [], "notice": notice, "text": text, "middle": middle,
        "subject_row": subject_row, "settings": settings, "default_name": gf.default_name(subject_row, subject_name, cmd),
        "count": len(selected), "download": "?" + get.urlencode() + "&download=1" if text else None})
