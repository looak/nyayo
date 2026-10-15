import uuid
import zlib

from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import chessboard, compare
from .ingest import IngestError, ingest
from .models import KINDS, STATES, Machine, Run


def text(status, body):
    return HttpResponse(body + "\n", status=status, content_type="text/plain; charset=utf-8")


def decompress(body, encoding):
    """The request body, gunzipped when the client said so, refusing anything that inflates past the limit."""
    encoding = (encoding or "identity").strip().lower()
    if encoding == "identity":
        return body
    if encoding != "gzip":
        raise IngestError(400, f"unsupported Content-Encoding {encoding}")
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(body, settings.NYAYO_MAX_RUN_BYTES)
    except zlib.error as error:
        raise IngestError(400, f"bad gzip body: {error}")
    if inflater.unconsumed_tail:
        raise IngestError(400, f"run is larger than {settings.NYAYO_MAX_RUN_BYTES} bytes")
    return data


@csrf_exempt
@require_http_methods(["PUT"])
def api_run(request, run_id):
    """PUT /api/runs/{id}: the whole run, created (201) or promoted (200). Errors are a short plain-text reason."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    machine = Machine.for_token(token.strip()) if scheme.lower() == "bearer" else None
    if machine is None:
        response = text(401, "missing, unknown or revoked token")
        response["WWW-Authenticate"] = "Bearer"
        return response
    try:
        raw = decompress(request.body, request.headers.get("Content-Encoding"))
        result = ingest(raw, machine, run_id)
    except IngestError as error:
        return text(error.status, error.reason)
    return text(201 if result.created else 200, "created" if result.created else "updated")


# --- pages ---

def run_queryset():
    return Run.objects.select_related("machine").prefetch_related("checks__metrics")


def runs_list(request):
    runs = run_queryset().order_by("-recorded_at")
    branch, state, kind = request.GET.get("branch", ""), request.GET.get("state", ""), request.GET.get("kind", "")
    if branch:
        runs = runs.filter(branch=branch)
    if state in STATES:
        runs = runs.filter(state=state)
    if kind:
        runs = runs.filter(kind=kind)
    kinds = KINDS + sorted(set(Run.objects.values_list("kind", flat=True)) - set(KINDS))
    page = Paginator(runs, 50).get_page(request.GET.get("page"))
    return render(request, "runs/runs_list.html", {
        "page": page, "branch": branch, "state": state, "states": STATES, "kind": kind, "kinds": kinds,
        "branches": Run.objects.order_by("branch").values_list("branch", flat=True).distinct(),
    })


def run_detail(request, run_id):
    run = get_object_or_404(run_queryset(), pk=run_id)
    return render(request, "runs/run_detail.html", {
        "run": run, "comparison": compare.compare_with_baselines(run),
        "previous": compare.previous_on_branch(run),
    })


def compare_page(request):
    """Run a against b: b is a run id, 'baseline' (each check against its own) or 'previous' (on a's branch)."""
    recent = list(Run.objects.order_by("-recorded_at")[:200])
    a_id, b_choice = request.GET.get("a"), request.GET.get("b") or "baseline"
    context = {"recent": recent, "a_id": a_id or "", "b_choice": b_choice}
    if not a_id:
        return render(request, "runs/compare.html", context)
    if not is_uuid(a_id):
        raise Http404("no such run")
    a = context["a"] = get_object_or_404(run_queryset(), pk=a_id)
    if b_choice == "baseline":
        context["comparison"] = compare.compare_with_baselines(a)
    else:
        b = compare.previous_on_branch(a) if b_choice == "previous" else \
            run_queryset().filter(pk=b_choice).first() if is_uuid(b_choice) else None
        if b is None:
            context["missing_b"] = "no previous run on this branch" if b_choice == "previous" else "no such run"
        else:
            context["b"] = b
            context["relation"] = compare.relation(a, b)
            context["comparison"] = compare.compare_runs(a, b)
    return render(request, "runs/compare.html", context)


def is_uuid(value):
    try:
        uuid.UUID(value)
        return True
    except (TypeError, ValueError):
        return False


def case_page(request, check_id, case_id):
    branches = list(Run.objects.order_by("branch").values_list("branch", flat=True).distinct())
    branch = request.GET.get("branch") or ("main" if "main" in branches else
                                           Run.objects.values_list("branch", flat=True).first() or "main")
    head, series = compare.case_history(check_id, case_id, branch)
    chosen = request.GET.get("series")
    current = next((s for s in series if s.key == chosen), series[0] if series else None)
    if not series and not Run.objects.filter(checks__check_id=check_id, checks__cases__case_id=case_id).exists():
        raise Http404("no such position")
    position = current.rows[0].case if current and current.rows else None
    board = position and chessboard.board_svg(position.fen, position.expected, position.avoid)
    return render(request, "runs/case.html", {
        "check_id": check_id, "case_id": case_id, "branch": branch, "branches": branches, "head": head,
        "series": series, "current": current, "summary": current.summary() if current else None,
        "position": position, "board": mark_safe(board) if board else None,
    })
