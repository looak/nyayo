from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from nyayo.runs.ingest import IngestError
from nyayo.runs.views import decompress, machine_for, text, unauthorized

from . import export, games, uploads
from .ingest import ingest
from .models import Match


@csrf_exempt
@require_http_methods(["PUT"])
def api_match(request, match_id):
    """PUT /api/matches/{id}: the whole match document, created (201) or updated (200). Errors are a short
    plain-text reason, as for runs."""
    machine = machine_for(request)
    if machine is None:
        return unauthorized()
    try:
        raw = decompress(request.body, request.headers.get("Content-Encoding"))
        result = ingest(raw, machine, match_id)
    except IngestError as error:
        response = text(error.status, error.reason)
        if error.status == 409:
            held = Match.objects.filter(pk=match_id).values_list("report_seq", "schedule_digest").first()
            if held:
                say_what_it_holds(response, *held)
        return response
    response = text(201 if result.created else 200, "created" if result.created else "updated")
    say_what_it_holds(response, result.match.report_seq, result.match.schedule_digest)
    return response


def say_what_it_holds(response, seq, schedule_digest):
    """Nyayo-Seq and Nyayo-Schedule-Digest on the answer, once nyayo holds a seq for the match (docs/match-schema.md,
    Reports by changes); a producer sends changes only after an answer carried them."""
    if seq is not None:
        response["Nyayo-Seq"] = str(seq)
        response["Nyayo-Schedule-Digest"] = schedule_digest


@csrf_exempt
@require_http_methods(["GET", "PUT"])
def api_game(request, match_id, game):
    """PUT /api/matches/{id}/games/{game}: the PGN so far, replacing the last (201 first time, 200 after). GET is the
    stored PGN, open like the pages so a viewer can poll it."""
    if request.method == "GET":
        pgn = games.read_pgn(match_id, game)
        if pgn is None:
            return text(404, "no such game")
        return HttpResponse(pgn, content_type="application/x-chess-pgn; charset=utf-8")
    machine = machine_for(request)
    if machine is None:
        return unauthorized()
    try:
        raw = decompress(request.body, request.headers.get("Content-Encoding"))
        created = games.store(match_id, game, raw)
    except IngestError as error:
        return text(error.status, error.reason)
    return text(201 if created else 200, "created" if created else "updated")


@csrf_exempt
@require_http_methods(["PUT"])
def api_batch(request, match_id, batch):
    """PUT /api/matches/{id}/pgns/{batch}: an upload of finished games, bz2 or PGN, taken without reading it. 202
    queued for the worker, 200 an identical resend, 400 a bad name or type, 404 an unknown match, 409 different
    content under a stored name, 413 too large."""
    machine = machine_for(request)
    if machine is None:
        return unauthorized()
    try:
        body = request.body
    except RequestDataTooBig:
        return text(413, "batch is too large")
    try:
        status = uploads.store(match_id, batch, body, request.headers.get("Content-Type"),
                               request.headers.get("Content-Encoding"), machine)
    except IngestError as error:
        return text(error.status, error.reason)
    return text(status, "accepted" if status == 202 else "unchanged")


@require_http_methods(["GET"])
def api_games(request):
    """GET /api/games?...: every stored game matching the filters (docs/match-schema.md, Export) as one bz2 stream,
    open like the pages."""
    try:
        filters = export.parse(request.GET)
    except export.BadFilter as error:
        return text(400, str(error))
    response = StreamingHttpResponse(export.chunks(filters), content_type="application/x-bzip2")
    response["Content-Disposition"] = 'attachment; filename="nyayo-games.pgn.bz2"'
    return response
