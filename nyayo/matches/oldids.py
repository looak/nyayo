"""Links to the ids matches had before `manage.py rename_matches` (openbench-openbench-1000, now sprt-1000).

A GET or HEAD of /matches/<old id> or anything under it, and of /api/matches/<old id>/... (the games a viewer polls),
answers 301 to the same path with the new id and the same query string. Only an id the command recorded
(`RenamedMatch`) redirects: one that merely looks like the old form, `openbench-nothing-7`, is a 404 as ever, and so is
an old id a match still holds under its own name (an outside producer's; ids are checked for that first).

A write to /api/matches/<old id>... is answered 409 naming the new id, not redirected: the document says its match.id
in its body, and a 308 would send the old one on to a url that disagrees with it (400), a producer that doesn't follow
redirects would get a bare 308 it can't read, and silently re-pointing a producer isn't ours to do. Nothing nyayo
runs writes under an old id: the tests' reports are built in this process with the new one. A producer told 409 can be
fixed to use the new id; one that sends the old id of a match that was never renamed makes a match of that id, as before.
"""

import re

from django.http import HttpResponse, HttpResponsePermanentRedirect

from nyayo.lab.ids import OLD_FORM

from .models import Match, RenamedMatch

# the id in a path after the script prefix: /matches/<id>[/...] or /api/matches/<id>[/...]
PATH = re.compile(r"^(?P<head>/(?:api/)?matches/)(?P<id>[^/]+)(?P<rest>/.*)?$")


def new_id_for(old_id):
    """The id a match was renamed to from old_id, or None: not an id the command renamed, or one a match holds still."""
    if not OLD_FORM.match(old_id):
        return None
    if Match.objects.filter(pk=old_id).exists():
        return None
    return RenamedMatch.objects.filter(pk=old_id).values_list("new_id", flat=True).first()


class OldMatchIds:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        found = PATH.match(request.path_info)
        if found and found["id"].startswith("openbench-"):
            new = new_id_for(found["id"])
            if new is not None:
                if request.method in ("GET", "HEAD"):
                    return self.redirect(request, found, new)
                if found["head"].startswith("/api/") and request.method in ("PUT", "PATCH"):
                    return HttpResponse(f"match {found['id']} is now {new}, send it there\n", status=409,
                                        content_type="text/plain; charset=utf-8")
        return self.get_response(request)

    @staticmethod
    def redirect(request, found, new):
        prefix = request.path[:len(request.path) - len(request.path_info)]
        target = f"{prefix}{found['head']}{new}{found['rest'] or ''}"
        query = request.META.get("QUERY_STRING", "")
        return HttpResponsePermanentRedirect(f"{target}?{query}" if query else target)
