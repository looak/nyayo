"""What OpenBench's Config/config.json said and the worker asks the server for: the version of its client and where to
get it, and the fastchess it needs. Settings in nyayo, each overridable in settings.py or by the environment. The rest
of that file (login, registration, cross approval, X-Accel) went with the pages and the users it was for."""

import os

from django.conf import settings


def setting(name, default):
    return getattr(settings, name, None) or os.environ.get(name) or default


def client_version():
    return int(setting("NYAYO_CLIENT_VERSION", 54))


def client_repo_url():
    return setting("NYAYO_CLIENT_REPO_URL", "https://github.com/looak/openbench_eg")


def client_repo_ref():
    return setting("NYAYO_CLIENT_REPO_REF", "master")


def fastchess_min_version():
    return setting("NYAYO_FASTCHESS_MIN_VERSION", "1.8.1")


def fastchess_repo_url():
    return setting("NYAYO_FASTCHESS_REPO_URL", "https://github.com/AndyGrant/fastchess")


def fastchess_repo_ref():
    return setting("NYAYO_FASTCHESS_REPO_REF", "master")


def balance_engine_throughputs():
    return False
