#!/usr/bin/env bash
# Copies the current nyayo's data into the data directory of a nyayo 2.0, to try it or to switch to it. Run from the 2.0
# checkout, with its conda env (nyayo2) active or NYAYO_BIN set. It only reads the current nyayo, with one exception: it runs
# the current checkout's `manage.py backup`, which writes `backup/nyayo.sqlite3` in the current data directory (what the daily
# backup timer does) and, because NYAYO_BACKUP_REPO is emptied for it, nothing else: no commit, no push.
#
#   OLD_CHECKOUT=/home/fischer/dev/nyayo OLD_BIN=/home/fischer/miniconda3/envs/nyayo/bin \
#   NEW_DATA=/home/fischer/dev/nyayo-2-data deploy/copy-live-data.sh
#
# What is copied: the database (the online copy `backup` made, never the live file), `archive/` and `games/` (without
# `games/batches`). Not copied: the batches and their index (see docs/deploy.md), `uploads/`, `datasets/`, `static/`, `backup/`.
# It refuses a new data directory that already holds a database, an archive or games, and the current data directory itself.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

OLD_CHECKOUT="${OLD_CHECKOUT:-}"
OLD_BIN="${OLD_BIN:-}"
NEW_DATA="${NEW_DATA:-}"
for name in OLD_CHECKOUT OLD_BIN NEW_DATA; do
    [ -n "${!name}" ] || { echo "$name is needed (see the top of this script)" >&2; exit 1; }
done
[ -f "$OLD_CHECKOUT/manage.py" ] || { echo "$OLD_CHECKOUT is not a nyayo checkout" >&2; exit 1; }
[ -x "$OLD_BIN/python" ] || { echo "no python in $OLD_BIN" >&2; exit 1; }
command -v rsync >/dev/null 2>&1 || { echo "rsync is not installed (sudo apt install rsync)" >&2; exit 1; }
case "$NEW_DATA" in /*) ;; *) echo "NEW_DATA must be an absolute path" >&2; exit 1 ;; esac

# where the current nyayo keeps its data, as it will say itself (its .env is read)
OLD_DATA="$(cd "$OLD_CHECKOUT" && "$OLD_BIN/python" -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'nyayo.settings')
django.setup()
from django.conf import settings
print(settings.DATA_DIR)
" 2>/dev/null | tail -n 1)"
[ -d "$OLD_DATA" ] || { echo "could not find the current data directory (got '$OLD_DATA')" >&2; exit 1; }
[ "$(readlink -f "$OLD_DATA")" != "$(readlink -f "$NEW_DATA")" ] || { echo "NEW_DATA is the current nyayo's data directory" >&2; exit 1; }

if [ -e "$NEW_DATA/nyayo.sqlite3" ] || [ -n "$(ls -A "$NEW_DATA/archive" "$NEW_DATA/games" 2>/dev/null)" ]; then
    echo "$NEW_DATA already holds a database, an archive or games. Move it aside first: mv $NEW_DATA $NEW_DATA.old" >&2
    exit 1
fi

echo "The current data directory: $OLD_DATA ($(du -sh "$OLD_DATA" | cut -f1))"
echo "A safe copy of its database (no commit, no push):"
(cd "$OLD_CHECKOUT" && NYAYO_BACKUP_REPO= "$OLD_BIN/python" manage.py backup)
SNAPSHOT="$OLD_DATA/backup/nyayo.sqlite3"
[ -s "$SNAPSHOT" ] || { echo "backup did not leave $SNAPSHOT" >&2; exit 1; }

mkdir -p "$NEW_DATA"
cp "$SNAPSHOT" "$NEW_DATA/nyayo.sqlite3"
for tree in archive games; do
    if [ -d "$OLD_DATA/$tree" ]; then
        mkdir -p "$NEW_DATA/$tree"
        # games/batches is where finished games are kept if it is in the data directory: never copied here
        rsync -a --exclude=/batches "$OLD_DATA/$tree/" "$NEW_DATA/$tree/"
    fi
done

echo
echo "Copied into $NEW_DATA:"
du -sh "$NEW_DATA"/nyayo.sqlite3 "$NEW_DATA"/archive "$NEW_DATA"/games 2>/dev/null | sed 's/^/    /'
echo "    matches in the copy: $("$NYAYO_BIN/python" -c "
import sqlite3, sys
db = sqlite3.connect('$NEW_DATA/nyayo.sqlite3')
print(db.execute('select count(*) from matches_match').fetchone()[0])
")"
echo "Next: migrate (docs/deploy.md)."
