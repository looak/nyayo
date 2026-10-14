#!/usr/bin/env bash
# Installs or reinstalls nyayo as a systemd service, with the unit's paths filled in for this checkout and conda env.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

unit="$(mktemp)"
trap 'rm -f "$unit"' EXIT
sed -e "s#^User=.*#User=$(id -un)#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
    -e "s#^ExecStart=[^ ]*/gunicorn#ExecStart=$NYAYO_BIN/gunicorn#" \
    "$REPO_DIR/deploy/nyayo.service" > "$unit"

echo "Installing /etc/systemd/system/nyayo.service:"
grep -E '^(User|WorkingDirectory|ExecStart)=' "$unit"

sudo install -m 644 "$unit" /etc/systemd/system/nyayo.service
sudo systemctl daemon-reload
sudo systemctl enable --now nyayo
sudo systemctl restart nyayo
systemctl status nyayo --no-pager
echo
echo "Follow the log with: journalctl -u nyayo -f"
