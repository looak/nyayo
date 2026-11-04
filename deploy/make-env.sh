#!/usr/bin/env bash
# Writes the .env of a nyayo 2.0 that runs beside another: its own data directory and port, a new secret key, debug off, and
# NYAYO_BACKUP_REPO left out. What the current nyayo's .env says of hosts and of the reader and the time zone is copied (never
# its data, batches, backup, secret or port). It never overwrites a .env.
#
#   NYAYO_DATA_DIR=/home/fischer/dev/nyayo-2-data \
#   FROM_ENV=/home/fischer/dev/nyayo/.env \
#   NYAYO_BATCHES_DIR=/mnt/nyayo-batches/batches-2 NYAYO_DATASETS_DIR=/mnt/nyayo-batches/datasets-2 \
#   deploy/make-env.sh                    port 8002 by default (NYAYO_PORT)
#
# NYAYO_BACKUP_REPO is given only with BACKUP_REPO=<checkout>, which is for the switch, not the trial.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

TARGET="$REPO_DIR/.env"
PORT="${NYAYO_PORT:-8002}"
DATA="${NYAYO_DATA_DIR:-}"
FROM="${FROM_ENV:-}"

[ -n "$DATA" ] || { echo "NYAYO_DATA_DIR is needed, as an absolute path" >&2; exit 1; }
case "$DATA" in /*) ;; *) echo "NYAYO_DATA_DIR must be an absolute path, not '$DATA'" >&2; exit 1 ;; esac
case "$PORT" in ''|*[!0-9]*) echo "NYAYO_PORT must be a number, not '$PORT'" >&2; exit 1 ;; esac
[ ! -e "$TARGET" ] || { echo "$TARGET exists already; this does not overwrite it" >&2; exit 1; }
if [ -n "$FROM" ] && [ ! -f "$FROM" ]; then echo "no $FROM to copy from" >&2; exit 1; fi

# the value of a setting in the current .env, or nothing
from() { [ -n "$FROM" ] && grep -E "^$1=" "$FROM" | tail -n 1 | cut -d= -f2- || true; }

# batches may not be where the current nyayo keeps them, and the datasets default to a directory beside them
if [ -n "$FROM" ] && [ -n "${NYAYO_BATCHES_DIR:-}" ]; then
    CURRENT="$(from NYAYO_BATCHES_DIR)"
    if [ -n "$CURRENT" ] && [ "$(readlink -f "$CURRENT" 2>/dev/null || echo "$CURRENT")" = "$(readlink -f "$NYAYO_BATCHES_DIR" 2>/dev/null || echo "$NYAYO_BATCHES_DIR")" ]; then
        echo "NYAYO_BATCHES_DIR is the current nyayo's batches ($CURRENT). 2.0 must never use them: its delete removes the files." >&2
        exit 1
    fi
fi
if [ -n "${NYAYO_BATCHES_DIR:-}" ] && [ -z "${NYAYO_DATASETS_DIR:-}" ]; then
    echo "with NYAYO_BATCHES_DIR, give NYAYO_DATASETS_DIR too: it defaults to a directory beside the batches, which here would be the current nyayo's" >&2
    exit 1
fi

HOSTS="$(from NYAYO_ALLOWED_HOSTS)"
HOSTS="${HOSTS:-${NYAYO_ALLOWED_HOSTS:-localhost,127.0.0.1}}"
ORIGINS=""
IFS=',' read -ra LIST <<< "$HOSTS"
for host in "${LIST[@]}"; do
    case "$host" in localhost|127.*|'') continue ;; esac
    ORIGINS="${ORIGINS:+$ORIGINS,}http://$host:$PORT"
done

SECRET="$("$NYAYO_BIN/python" -c 'import secrets; print(secrets.token_urlsafe(50))')"

{
    echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ), by deploy/make-env.sh: nyayo 2.0 on port $PORT"
    echo "NYAYO_DEBUG=false"
    echo "NYAYO_SECRET_KEY=$SECRET"
    echo "NYAYO_ALLOWED_HOSTS=$HOSTS"
    echo "NYAYO_CSRF_TRUSTED_ORIGINS=$ORIGINS"
    echo "NYAYO_DATA_DIR=$DATA"
    [ -z "${NYAYO_BATCHES_DIR:-}" ]  || echo "NYAYO_BATCHES_DIR=$NYAYO_BATCHES_DIR"
    [ -z "${NYAYO_DATASETS_DIR:-}" ] || echo "NYAYO_DATASETS_DIR=$NYAYO_DATASETS_DIR"
    [ -z "${BACKUP_REPO:-}" ]        || echo "NYAYO_BACKUP_REPO=$BACKUP_REPO"
    # what the current nyayo is set up to do, which does not depend on which of them runs
    for key in NYAYO_INSTANCE NYAYO_TIME_ZONE NYAYO_READER NYAYO_READER_SHA256 NYAYO_LOG_LEVEL NYAYO_GITHUB_REPO NYAYO_GITHUB_TOKEN \
               NYAYO_GAME_THREADS NYAYO_SILENT_AFTER NYAYO_FAILED_PRECHECKS_HOURS NYAYO_MIN_PLY_DEFAULT NYAYO_BOOK_MIN_PLY; do
        value="$(from "$key")"
        [ -z "$value" ] || echo "$key=$value"
    done
} > "$TARGET"
chmod 600 "$TARGET"

echo "Wrote $TARGET (the secret key is not shown):"
grep -v '^NYAYO_SECRET_KEY=' "$TARGET" | grep -v '^NYAYO_GITHUB_TOKEN=' | sed 's/^/    /'
