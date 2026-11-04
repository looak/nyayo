#!/usr/bin/env bash
# Installs a second nyayo beside the one that is running, as the systemd units nyayo2 and nyayo2-worker, from this checkout and
# this conda env (nyayo2 unless NYAYO_CONDA_ENV says otherwise), on its own port. It writes the units from deploy/nyayo.service
# and deploy/nyayo-worker.service, renamed, and installs nothing else: no expire or backup timer, and nothing of today's nyayo,
# nyayo-worker or their timers is touched. install-service.sh is still the script that installs the real ones.
#
#   deploy/install-side-by-side.sh                 8002, two gunicorn workers of four threads
#   NYAYO_PORT=8003 NYAYO_WORKERS=1 deploy/install-side-by-side.sh
#   DRY_RUN=1 deploy/install-side-by-side.sh       writes the units to ./side-by-side-units and shows them; no sudo, nothing installed
#
# It refuses the name nyayo, a port that is not a number, and a unit that is already installed (UNIT_FORCE=1 reinstalls it).
set -euo pipefail

: "${NYAYO_CONDA_ENV:=nyayo2}"
export NYAYO_CONDA_ENV
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

UNIT="${NYAYO_UNIT:-nyayo2}"
PORT="${NYAYO_PORT:-8002}"
WORKERS="${NYAYO_WORKERS:-2}"
DEST=/etc/systemd/system

case "$UNIT" in
    nyayo|nyayo-*) echo "refusing the unit name '$UNIT': it is today's nyayo's (or one of its timers'). Use another, e.g. nyayo2." >&2; exit 1 ;;
esac
case "$PORT" in ''|*[!0-9]*) echo "NYAYO_PORT must be a number, not '$PORT'" >&2; exit 1 ;; esac
case "$WORKERS" in ''|*[!0-9]*) echo "NYAYO_WORKERS must be a number, not '$WORKERS'" >&2; exit 1 ;; esac
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "no $REPO_DIR/.env: make it first (deploy/make-env.sh), the service reads it" >&2
    exit 1
fi

if [ -n "${DRY_RUN:-}" ]; then
    OUT="${OUT:-$PWD/side-by-side-units}"
    mkdir -p "$OUT"
else
    OUT="$(mktemp -d)"
    trap 'rm -rf "$OUT"' EXIT
    for unit in "$UNIT.service" "$UNIT-worker.service"; do
        if [ -e "$DEST/$unit" ] && [ -z "${UNIT_FORCE:-}" ]; then
            echo "$DEST/$unit exists already; UNIT_FORCE=1 replaces it" >&2
            exit 1
        fi
    done
fi

# the site: its own name, port and number of gunicorn workers
sed -e "s#^Description=.*#Description=nyayo 2.0 beside the current one, port $PORT#" \
    -e "s#^User=.*#User=$(id -un)#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
    -e "s#^ExecStart=[^ ]*/bin/#ExecStart=$NYAYO_BIN/#" \
    -e "s#--bind 0.0.0.0:[0-9]*#--bind 0.0.0.0:$PORT#" \
    -e "s#--workers [0-9]*#--workers $WORKERS#" \
    "$REPO_DIR/deploy/nyayo.service" > "$OUT/$UNIT.service"

# its worker, bound to it and not to today's nyayo.service
sed -e "s#nyayo\\.service#$UNIT.service#g" \
    -e "s#^Description=.*#Description=nyayo 2.0 beside the current one: put away the games kept as data#" \
    -e "s#^User=.*#User=$(id -un)#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
    -e "s#^ExecStart=[^ ]*/bin/#ExecStart=$NYAYO_BIN/#" \
    "$REPO_DIR/deploy/nyayo-worker.service" > "$OUT/$UNIT-worker.service"

for unit in "$UNIT.service" "$UNIT-worker.service"; do
    echo "== $DEST/$unit"
    grep -E '^(Description|User|WorkingDirectory|ExecStart|After|BindsTo|PartOf|WantedBy)=' "$OUT/$unit" | sed 's/^/    /'
done

if [ -n "${DRY_RUN:-}" ]; then
    echo "Dry run: the units are in $OUT, nothing was installed."
    exit 0
fi

for unit in "$UNIT.service" "$UNIT-worker.service"; do
    sudo install -m 644 "$OUT/$unit" "$DEST/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT" "$UNIT-worker"
systemctl status "$UNIT" "$UNIT-worker" --no-pager
echo
echo "Follow the log with: journalctl -u $UNIT -f"
