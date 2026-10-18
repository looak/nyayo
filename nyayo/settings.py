"""Django settings for nyayo.

Everything that differs between the dev PC and bobby comes from the environment or an untracked .env file in the
repository root; see .env.example.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("NYAYO_DEBUG")

SECRET_KEY = os.environ.get("NYAYO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("NYAYO_SECRET_KEY must be set when NYAYO_DEBUG is off")
    SECRET_KEY = "insecure-dev-only-key"

ALLOWED_HOSTS = env_list("NYAYO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("NYAYO_CSRF_TRUSTED_ORIGINS")

# Set when a reverse proxy serves nyayo under a path, e.g. /nyayo. Empty when it has its own port.
FORCE_SCRIPT_NAME = os.environ.get("NYAYO_SCRIPT_NAME") or None

# The database and the raw results archive live together so one directory holds all state.
DATA_DIR = Path(os.environ.get("NYAYO_DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Raw results as uploaded, a file per run id; the database is an index of these.
NYAYO_ARCHIVE_DIR = DATA_DIR / "archive"

# Games of matches, one PGN file per game under <match id>/; apart from the archive so they can be purged on their own
# (manage.py purge_games) without touching a match's results.
NYAYO_GAMES_DIR = DATA_DIR / "games"

# How often a game page asks whether its game has moved. The answer to "nothing new" is empty and costs a stat and a
# one row query, so this can be fast; raise it if nyayo's workers are busy with many viewers.
NYAYO_GAME_POLL_MS = int(os.environ.get("NYAYO_GAME_POLL_MS", 100))

# Runs that never became history expire (manage.py expire_runs): wip runs this many days after being recorded,
# committed runs this many days after being committed if they weren't published by then.
NYAYO_WIP_DAYS = int(os.environ.get("NYAYO_WIP_DAYS", 14))
NYAYO_COMMITTED_DAYS = int(os.environ.get("NYAYO_COMMITTED_DAYS", 30))

# A git checkout (a private repository, cloned there) that manage.py backup commits the archive, the games and a dump of
# the database to. Unset, backup only makes a local copy of the database.
NYAYO_BACKUP_REPO = os.environ.get("NYAYO_BACKUP_REPO", "")

# A running match with no report for this many minutes is shown as silent (derived when a page is drawn, never stored).
NYAYO_SILENT_AFTER = int(os.environ.get("NYAYO_SILENT_AFTER", 30))

# A run's largest size once decompressed; uploads are gzipped and far smaller on the wire.
NYAYO_MAX_RUN_BYTES = 64 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024 * 1024

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "nyayo",
    "nyayo.runs",
    "nyayo.matches",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "nyayo.urls"
WSGI_APPLICATION = "nyayo.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "nyayo.sqlite3",
        "OPTIONS": {
            # WAL lets pages read while the poller or an upload writes; IMMEDIATE avoids "database is locked" upgrades.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

# Spelled out with the prefix: Django's automatic prefixing of a relative STATIC_URL depends on when settings are
# first read, which makes static files under a prefix unreliable.
STATIC_URL = f"{FORCE_SCRIPT_NAME.rstrip('/')}/static/" if FORCE_SCRIPT_NAME else "/static/"
STATIC_ROOT = DATA_DIR / "static"
STATIC_ROOT.mkdir(exist_ok=True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
