#!/usr/bin/env bash
# Pulls the latest nyayo, installs its dependencies, migrates the database and restarts the service.
# Wrapped in a function so bash has read the whole script before git pull can change it.
set -euo pipefail

main() {
    source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
    cd "$REPO_DIR"

    git pull --ff-only
    "$NYAYO_BIN/python" -m pip install --quiet -e .
    "$NYAYO_BIN/python" manage.py migrate
    "$NYAYO_BIN/python" manage.py collectstatic --noinput --verbosity 0
    sudo systemctl restart nyayo nyayo-worker
    systemctl status nyayo nyayo-worker --no-pager

    # What the next test will be called, so a wrong NYAYO_INSTANCE is seen before a test is made with it
    "$NYAYO_BIN/python" manage.py shell --no-imports -c "
from django.conf import settings
from django.db import connection
from nyayo.lab.ids import match_id
row = connection.cursor().execute(\"select seq from sqlite_sequence where name = 'lab_test'\").fetchone()
print('The next test is', match_id((row[0] if row else 0) + 1), '(NYAYO_INSTANCE=%s)' % settings.NYAYO_INSTANCE)
" 2>/dev/null
}

main "$@"
