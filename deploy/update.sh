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
    # the units to restart: nyayo's own, unless NYAYO_UNITS names others (a second nyayo on the machine, with units of other names)
    units="${NYAYO_UNITS:-nyayo nyayo-worker}"
    sudo systemctl restart $units
    systemctl status $units --no-pager
}

main "$@"
