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

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "nyayo",
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
