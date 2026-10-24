"""What the frame around every page shows: the sidebar's status block and its badges.

It is worked out only when a page's frame asks for it (a lazy object), so the game page's fragment, polled ten times a
second and rendered without the frame, costs nothing more.
"""

import socket
from pathlib import Path

from django.conf import settings
from django.db.models import Sum
from django.utils.functional import SimpleLazyObject


def status():
    from nyayo.matches import worker
    from nyayo.matches.models import Batch, Game, Match, Upload

    batches = Batch.objects.aggregate(games=Sum("games"), stored=Sum("stored_bytes"))
    uploads = {state: Upload.objects.filter(state=state).count() for state in ("pending", "refused")}
    return {
        "host": socket.gethostname(),
        "worker": worker.is_running(),
        "reader": Path(settings.NYAYO_READER).name if settings.NYAYO_READER else "python-chess",
        "games_kept": f"{batches['games'] or 0:,}",
        "stored_mb": round((batches["stored"] or 0) / 1e6),
        "batches_here": Path(settings.NYAYO_BATCHES_DIR).is_dir(),
        "uploads_waiting": uploads["pending"],
        "uploads_refused": uploads["refused"],
        "running": Match.objects.filter(state="running").count(),
        "in_play": Game.objects.filter(result="*", match__state="running").count(),
    }


def shell(request):
    return {"shell": SimpleLazyObject(status)}
