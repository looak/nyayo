"""Django settings for nyayo.

Everything that differs between the dev PC and bobby comes from the environment or an untracked .env file in the
repository root; see .env.example.
"""

import json
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

# How many of a gunicorn worker's threads may handle game requests (a game's moves, a batch of games) at once; the
# rest are kept for match reports and pages (nyayo/gamethreads.py). Below the --threads in deploy/nyayo.service.
NYAYO_GAME_THREADS = int(os.environ.get("NYAYO_GAME_THREADS", 2))

# Runs that never became history expire (manage.py expire_runs): wip runs this many days after being recorded,
# committed runs this many days after being committed if they weren't published by then.
NYAYO_WIP_DAYS = int(os.environ.get("NYAYO_WIP_DAYS", 14))
NYAYO_COMMITTED_DAYS = int(os.environ.get("NYAYO_COMMITTED_DAYS", 30))

# A git checkout (a private repository, cloned there) that manage.py backup commits the archive, the games and a dump of
# the database to. Unset, backup only makes a local copy of the database.
NYAYO_BACKUP_REPO = os.environ.get("NYAYO_BACKUP_REPO", "")

# Batches of finished games (games/batches/<match id>/<batch>.pgn.bz2) are data to keep, apart from the live games so
# purge_games never touches them; put them on the NAS's share by pointing this there. A batch is at most 64 MB as sent
# and 256 MB once decompressed.
NYAYO_BATCHES_DIR = Path(os.environ.get("NYAYO_BATCHES_DIR") or NYAYO_GAMES_DIR / "batches").resolve()

# Position datasets (datasets.py): the positions files the worker draws from the batches, next to the batches on the
# share by default. A dataset's record is in the database; its file can be deleted and made again.
NYAYO_DATASETS_DIR = Path(os.environ.get("NYAYO_DATASETS_DIR") or NYAYO_BATCHES_DIR.parent / "datasets").resolve()
# The first ply a dataset takes positions from, counted from where a game starts, by the match's opening book (its
# file's name without the extension, any case): 8moves_v3's positions are 8 moves in, so 4 more plies past the book;
# Endgames.epd's positions are wanted from the start. A book not named gets NYAYO_MIN_PLY_DEFAULT (owner, 2026-10-26).
NYAYO_BOOK_MIN_PLY = json.loads(os.environ.get("NYAYO_BOOK_MIN_PLY") or '{"8moves_v3": 4, "Endgames": 0}')
NYAYO_MIN_PLY_DEFAULT = int(os.environ.get("NYAYO_MIN_PLY_DEFAULT", 8))
NYAYO_MAX_BATCH_UPLOAD_BYTES = 64 * 1024 * 1024
# Games in one of nyayo's batches; every batch records its own count, so changing this needs nothing else.
NYAYO_BATCH_GAMES = int(os.environ.get("NYAYO_BATCH_GAMES", 64))
# A match's last batch is sealed short once the match has ended and nyayo has heard nothing of it for this long.
NYAYO_SEAL_AFTER = int(os.environ.get("NYAYO_SEAL_AFTER", 300))

# The game reader, elephant's phant-indexer: the path of its release binary and that release's sha256, which the
# worker checks before it starts (it refuses to start on a mismatch). Unset, games are read with python-chess.
NYAYO_READER = os.environ.get("NYAYO_READER", "")
NYAYO_READER_SHA256 = os.environ.get("NYAYO_READER_SHA256", "")

# Uploads of finished games wait here, as sent, until the worker has put their games in batches (then they're
# deleted). Not in the backup: a pending upload is the producer's to send again.
NYAYO_UPLOADS_DIR = DATA_DIR / "uploads"

# The index of every game kept as data, a card per game: its own SQLite database, derived from the batches and
# rebuilt from them, so it is on local disk (never on a network share, where SQLite's locking isn't safe) and not
# in the backup.
NYAYO_INDEX_DB = Path(os.environ.get("NYAYO_INDEX_DB") or DATA_DIR / "index.sqlite3").resolve()
NYAYO_MAX_BATCH_BYTES = 256 * 1024 * 1024

# A match whose producer reports by changes is archived whole this often while it runs (and when it ends), not on every
# report; 0 archives every report.
NYAYO_ARCHIVE_EVERY = int(os.environ.get("NYAYO_ARCHIVE_EVERY", 60))

# A running match with no report for this many minutes is shown as silent (derived when a page is drawn, never stored).
NYAYO_SILENT_AFTER = int(os.environ.get("NYAYO_SILENT_AFTER", 30))

# The engine's repository on GitHub, owner/name: the dashboard links its commits there. Unset, nothing links out.
NYAYO_GITHUB_REPO = os.environ.get("NYAYO_GITHUB_REPO", "").strip().strip("/")

# A run's largest size once decompressed; uploads are gzipped and far smaller on the wire.
NYAYO_MAX_RUN_BYTES = 64 * 1024 * 1024
# a batch of games may be up to NYAYO_MAX_BATCH_UPLOAD_BYTES as sent
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024 * 1024

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
    "nyayo.gamethreads.GameThreadsMiddleware",
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
                "nyayo.shell.shell",
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
# Times are stored in UTC and shown in this zone (an IANA name, e.g. America/Vancouver); the pages say which.
TIME_ZONE = os.environ.get("NYAYO_TIME_ZONE", "").strip() or "UTC"
USE_I18N = False
USE_TZ = True

# Spelled out with the prefix: Django's automatic prefixing of a relative STATIC_URL depends on when settings are
# first read, which makes static files under a prefix unreliable.
STATIC_URL = f"{FORCE_SCRIPT_NAME.rstrip('/')}/static/" if FORCE_SCRIPT_NAME else "/static/"
STATIC_ROOT = DATA_DIR / "static"
STATIC_ROOT.mkdir(exist_ok=True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# A server error's traceback goes to stderr, which systemd keeps: `journalctl -u nyayo`. Django's own setup only mails
# them to admins when DEBUG is off, and nothing is set up to mail, so a 500 used to leave one access-log line and no
# reason. Only errors (5xx) are logged here, not the 404s bots cause; NYAYO_LOG_LEVEL is for nyayo's own messages.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.server": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "nyayo": {"handlers": ["console"], "level": os.environ.get("NYAYO_LOG_LEVEL", "INFO").upper(),
                  "propagate": False},
    },
}
