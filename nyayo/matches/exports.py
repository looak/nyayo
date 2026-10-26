"""Position datasets on the web: the Exports page (`/exports`) and the API (`/api/exports`).

`POST /api/exports` with a machine's token requests one, so another agent starts a dataset with one request; `GET
/api/exports` lists them and `GET /api/exports/{id}` gives one's state, progress and record, with its file's URL once
it's done. Reading is open, like the pages; the page's buttons are forms, as the admin's are, on a LAN site without
logins: New dataset, and Rebuild for a dataset whose file was deleted.
"""

import json

from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from nyayo.runs.views import machine_for, text, unauthorized

from . import datasets
from .models import Dataset

NOTE_MAX = 2000


def described(dataset, request, manifest=False):
    """A dataset as the API gives it: its record, its progress, and its file's URL while there is one."""
    has_file = dataset.state == "done" and dataset.file_deleted_at is None
    out = {
        "id": dataset.pk, "state": dataset.state, "note": dataset.note, "requested_by": dataset.requested_by,
        "requested_at": stamp(dataset.requested_at), "started_at": stamp(dataset.started_at),
        "finished_at": stamp(dataset.finished_at), "selection": dataset.selection, "draw": dataset.draw,
        "progress": {"batches": dataset.batches_done, "of": dataset.batches_total, "games": dataset.games,
                     "positions": dataset.positions},
        "nyayo": dataset.nyayo_version, "reader": dataset.reader, "reader_sha256": dataset.reader_sha256,
        "sha256": dataset.sha256, "bytes": dataset.file_bytes, "file_deleted_at": stamp(dataset.file_deleted_at),
        "rebuild_of_sha256": dataset.rebuild_of_sha256 or None, "rebuild_matched": dataset.rebuild_matched,
        "error": dataset.error or None, "stats": dataset.stats,
        "file": request.build_absolute_uri(reverse("export_file", args=[dataset.pk])) if has_file else None,
    }
    if manifest:
        out["manifest"] = dataset.manifest
    return out


def stamp(when):
    return when.isoformat(timespec="seconds") if when else None


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_exports(request):
    """GET: every dataset, newest first. POST, with a machine's token: a new one, `{"note": ..., "merge": ...}`
    optional; 201."""
    if request.method == "GET":
        return JsonResponse({"exports": [described(d, request) for d in Dataset.objects.all()]})
    machine = machine_for(request)
    if machine is None:
        return unauthorized()
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return text(400, "the body must be JSON, or empty")
    if (not isinstance(body, dict) or not isinstance(body.get("note", ""), str)
            or not isinstance(body.get("merge", False), bool)):
        return text(400, 'the body is {"note": "...", "merge": false}, both optional')
    dataset = datasets.request(requested_by=machine.name, note=body.get("note", "")[:NOTE_MAX],
                               merge=body.get("merge", False))
    return JsonResponse(described(dataset, request), status=201)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def api_export(request, dataset_id):
    """GET: one dataset's record, progress and manifest. DELETE, with a machine's token: its file, the record kept
    (a rebuild makes the file again); 409 for a dataset not done."""
    dataset = get_object_or_404(Dataset, pk=dataset_id)
    if request.method == "DELETE":
        if machine_for(request) is None:
            return unauthorized()
        if dataset.state != "done":
            return text(409, f"dataset {dataset.pk} is {dataset.state}; only a finished one's file can be deleted")
        datasets.delete_file(dataset)
    return JsonResponse(described(dataset, request, manifest=True))


@require_http_methods(["GET"])
def export_file(request, dataset_id):
    """The positions, zstd: `<fen> [result]` lines."""
    dataset = get_object_or_404(Dataset, pk=dataset_id)
    path = datasets.file_path(dataset)
    if dataset.state != "done" or dataset.file_deleted_at is not None or not path.exists():
        raise Http404("no file for this dataset")
    return FileResponse(open(path, "rb"), as_attachment=True, filename=path.name, content_type="application/zstd")


@require_http_methods(["GET", "POST"])
def exports_page(request):
    """The Exports page (V1): every dataset, newest first, with its state, age, positions and whether its file is
    still there; the one in progress on top with its progress; a New dataset button, and Rebuild for a file
    deleted."""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "new":
            datasets.request(requested_by="page", merge=request.POST.get("merge") == "on")
        elif action == "rebuild":
            dataset = get_object_or_404(Dataset, pk=request.POST.get("id") or 0)
            if dataset.state == "done" and dataset.file_deleted_at is not None:
                datasets.rebuild(dataset)
        return redirect("exports")
    rows = list(Dataset.objects.all())
    for dataset in rows:
        dataset.stats_view = datasets.stats_view(dataset.stats)
        dataset.settings_view = datasets.settings_view(dataset.draw)
    current = next((d for d in rows if d.state == "running"), None) or next(
        (d for d in reversed(rows) if d.state == "requested"), None)
    return render(request, "matches/exports.html", {"datasets": rows, "current": current})


# the rings' colours: Pi-hole's, and the results' green, grey and red as everywhere else
RESULT_COLOURS = {"white wins": "#2ecc71", "draws": "#9aa4ad", "black wins": "#e5533d", "mixed": "#d98a12"}
GAME_COLOURS = {"used": "#12895a", "didn't read": "#c64a3a", "no result": "#6c757d", "a null move": "#7b5ea7"}
# four hues far apart, each clear of the ring's dark ground: what was written stands out, what wasn't recedes
POSITION_COLOURS = {"written": "#3498db", "duplicates": "#f39c12", "quiet, not drawn": "#9b59b6",
                    "not quiet": "#d0d6db"}
# what the quiet-per-game figures are, for their tooltips
QUIET_TIPS = {
    "mean": "the quiet positions past a game's first ply, averaged over the games used: how many each game had to "
            "draw its 10 from",
    "median": "half the games had fewer quiet positions than this, half more",
    "p5": "one game in twenty had fewer quiet positions than this: the short or tactical games, which give fewer "
          "than 10",
    "min": "the fewest quiet positions any game had",
}


def rings(found):
    """The export page's three rings, each from dashboard.donut(): the written positions' results, the games read,
    and the candidate positions: written, duplicates (dropped, or merged into one), quiet but not drawn, not
    quiet."""
    from .dashboard import donut

    def n(name):
        return found.get(name) or 0

    results = donut([(label, n(name), RESULT_COLOURS[label]) for label, name in
                     (("white wins", "white_wins"), ("draws", "draws"), ("black wins", "black_wins"),
                      ("mixed", "mixed"))])
    games = donut([("used", n("games_used"), GAME_COLOURS["used"]),
                   ("didn't read", n("unread"), GAME_COLOURS["didn't read"]),
                   ("no result", n("no_result"), GAME_COLOURS["no result"])])
    # merged: positions drawn, of which written distinct; else written, and duplicates passed over
    drawn = found.get("drawn")
    duplicates = (drawn - n("written")) if drawn is not None else n("duplicates")
    taken = drawn if drawn is not None else n("written") + n("duplicates")
    positions = donut([("written", n("written"), POSITION_COLOURS["written"]),
                       ("merged into one" if drawn is not None else "duplicates dropped", duplicates,
                        POSITION_COLOURS["duplicates"]),
                       ("quiet, not drawn", max(0, n("quiet") - taken), POSITION_COLOURS["quiet, not drawn"]),
                       ("not quiet", max(0, n("candidates") - n("quiet")), POSITION_COLOURS["not quiet"])])
    return {"results": results, "games": games, "positions": positions}


@require_http_methods(["GET"])
def export_page(request, dataset_id):
    """One dataset's page, as an SPRT's or a gauntlet's: tiles, its rings, the quiet positions per game, every
    statistic, its settings and its record."""
    dataset = get_object_or_404(Dataset, pk=dataset_id)
    stats = datasets.stats_view(dataset.stats)
    found = stats["found"] if stats else {}
    quiet = [(label, found.get(name), QUIET_TIPS[key]) for key, name, label in
             (("mean", "per_game_mean", "mean"), ("median", "per_game_median", "median"),
              ("p5", "per_game_p5", "5th percentile"), ("min", "per_game_min", "minimum"))]
    return render(request, "matches/export.html", {
        "d": dataset, "stats": stats, "rings": rings(found) if stats else None, "quiet": quiet,
        "settings": datasets.settings_view(dataset.draw), "has_file": dataset.state == "done"
        and dataset.file_deleted_at is None, "running": dataset.state in ("requested", "running"),
        "took": (dataset.finished_at - dataset.started_at).total_seconds()
        if dataset.started_at and dataset.finished_at else None,
    })
