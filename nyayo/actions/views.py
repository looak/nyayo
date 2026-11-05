"""Sign in and out, and the stubs: a page for each link of the Actions and Lab groups, saying what will be there.

A stub is replaced by its step's real view: change the line in `urls.py` that names it, and delete its entry in `STUBS`.
Whether a stub is behind the gate is part of the entry, so a real page keeps the same answer.
"""

from django import forms
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import render

from .access import action_required, may_act

# name -> (page's heading, step that builds it, what will be there, gated)
STUBS = {
    "create_tune": ("Create tune", 7, "The form that starts an SPSA tune, and the page of its parameters' history.", True),
    "create_datagen": ("Create datagen", 7, "The form that starts a data generation run. It is handed out like a test, and its page is the match page.", True),
    "upload_net": ("Upload net", 4, "Elephant has no network, so there is no upload page in 2.0. The fields and the worker's request for a network stay.", True),
    "gauntlet": ("Run gauntlet", 4, "The gauntlet page: the file to copy and run on a machine that plays it, as uwanja takes it.", True),
    "engines": ("Engines", 6, "What to build: a repository, a ref, build options, the reference NPS and the date it was set. A test copies its engine's values when it is created.", True),
    "books": ("Books", 6, "The opening books a test may use.", True),
}


def stub(name):
    heading, step, text, gated = STUBS[name]

    def view(request):
        return render(request, "actions/stub.html", {"here": name, "heading": heading, "step": step, "text": text,
                                                     "gated": gated, "may_act": may_act(request.user)})

    view.__name__ = f"stub_{name}"
    return action_required(view) if gated else view


class SignInForm(AuthenticationForm):
    """Django's own, for the admin's users; the page that sends a person here only knows staff may act."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError("This user can't sign in here.", code="not_staff")


class SignIn(auth_views.LoginView):
    template_name = "actions/sign_in.html"
    authentication_form = SignInForm
    redirect_authenticated_user = True
    extra_context = {"here": "sign_in"}


class SignOut(auth_views.LogoutView):
    """POST only, as Django has it: a link anyone could put in a page must not sign a person out."""
