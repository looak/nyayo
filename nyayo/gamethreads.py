"""Keeps game requests off some of each worker's threads, so a flood of them leaves status and pages answering.

Producers send a game's moves (PUT or PATCH .../games/{game}) and batches of finished games (PUT .../pgns/{batch})
far more often than anything else. Each gunicorn worker lets at most NYAYO_GAME_THREADS of its threads handle those at once; a
game request that finds them all taken is answered 503 with Retry-After at once, before its body is read, and the
producer sends it again later. Match reports (PUT /api/matches/{id}), the pages and everything else never wait for a
game slot. The count is per process: gunicorn's workers are processes, each with its own threads.
"""

import threading

from django.conf import settings

from nyayo.matches import views
from nyayo.runs.views import text

GAME_VIEWS = {views.api_game, views.api_batch}

_slots = threading.BoundedSemaphore(settings.NYAYO_GAME_THREADS)


class GameThreadsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        finally:
            if getattr(request, "_nyayo_game_slot", False):
                request._nyayo_game_slot = False
                _slots.release()

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method not in ("PUT", "PATCH") or view_func not in GAME_VIEWS:
            return None
        if not _slots.acquire(blocking=False):
            response = text(503, "busy with other games, send it again shortly")
            response["Retry-After"] = "1"
            response._has_been_logged = True    # expected under a flood, not a server error for the journal
            return response
        request._nyayo_game_slot = True
        return None
