"""Position datasets on the web: the Exports page (`/exports`) and the API (`/api/exports`).

`POST /api/exports` with a machine's token requests one, so another agent starts a dataset with one request; `GET
/api/exports` lists them and `GET /api/exports/{id}` gives one's state, progress and record, with its file's URL once
it's done. Reading is open, like the pages; the page's buttons are forms, as the admin's are, on a LAN site without
logins.
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
        "error": dataset.error or None,
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
    """GET: every dataset, newest first. POST, with a machine's token: a new one, `{"note": ...}` optional; 201."""
    if request.method == "GET":
        return JsonResponse({"exports": [described(d, request) for d in Dataset.objects.all()]})
    machine = machine_for(request)
    if machine is None:
        return unauthorized()
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return text(400, "the body must be JSON, or empty")
    if not isinstance(body, dict) or not isinstance(body.get("note", ""), str):
        return text(400, 'the body is {"note": "..."}, the note optional')
    dataset = datasets.request(requested_by=machine.name, note=body.get("note", "")[:NOTE_MAX])
    return JsonResponse(described(dataset, request), status=201)


@require_http_methods(["GET"])
def api_export(request, dataset_id):
    """GET: one dataset's record, progress and manifest."""
    return JsonResponse(described(get_object_or_404(Dataset, pk=dataset_id), request, manifest=True))


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
    """The Exports page: a button to request a dataset, and every dataset with its progress and record. Its forms
    request, note, rebuild and delete the file."""
    if request.method == "POST":
        action = request.POST.get("action")
        dataset = Dataset.objects.filter(pk=request.POST.get("id") or 0).first()
        if action == "request":
            datasets.request(requested_by="page", note=request.POST.get("note", "")[:NOTE_MAX])
        elif dataset is None:
            raise Http404("no such dataset")
        elif action == "note":
            dataset.note = request.POST.get("note", "")[:NOTE_MAX]
            dataset.save(update_fields=["note"])
        elif action == "rebuild" and dataset.state == "done":
            datasets.rebuild(dataset)
        elif action == "delete" and dataset.state == "done":
            datasets.delete_file(dataset)
        return redirect("exports")
    rows = list(Dataset.objects.all())
    busy = any(d.state in ("requested", "running") for d in rows)
    return render(request, "matches/exports.html", {"datasets": rows, "busy": busy, "draw": datasets.DRAW})
