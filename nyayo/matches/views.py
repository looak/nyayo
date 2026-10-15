from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from nyayo.runs.ingest import IngestError
from nyayo.runs.views import decompress, machine_for, text, unauthorized

from .ingest import ingest


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
        return text(error.status, error.reason)
    return text(201 if result.created else 200, "created" if result.created else "updated")
