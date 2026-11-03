"""Who may act. Reading is open on the LAN; anything that starts, changes or stops work needs a signed-in admin user.

Every page that acts is wrapped in `action_required` (a function view) or inherits `ActionRequiredMixin` (a class view),
and nothing else decides it, so a later page can't forget a check by inventing its own. Signed out, a person is sent to
the sign-in page and back; signed in without being staff, they get a plain 403, as the admin would. The API routes with
machine tokens don't come through here: they have their own tokens.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def may_act(user):
    return user.is_authenticated and user.is_active and user.is_staff


def action_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not may_act(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


class ActionRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not may_act(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)
