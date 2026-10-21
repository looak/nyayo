#!/usr/bin/env bash
# Installs or reinstalls nyayo as a systemd service, with its worker and the daily timers, with the units' paths
# filled in for this checkout, user and conda env.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
for unit in nyayo.service nyayo-worker.service nyayo-expire.service nyayo-expire.timer nyayo-backup.service nyayo-backup.timer; do
    sed -e "s#^User=.*#User=$(id -un)#" \
        -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
        -e "s#^ExecStart=[^ ]*/bin/#ExecStart=$NYAYO_BIN/#" \
        "$REPO_DIR/deploy/$unit" > "$work/$unit"
    echo "Installing /etc/systemd/system/$unit"
    grep -E '^(User|WorkingDirectory|ExecStart|OnCalendar)=' "$work/$unit" | sed 's/^/    /'
    sudo install -m 644 "$work/$unit" "/etc/systemd/system/$unit"
done

sudo systemctl daemon-reload
sudo systemctl enable --now nyayo nyayo-worker nyayo-expire.timer nyayo-backup.timer
sudo systemctl restart nyayo nyayo-worker
systemctl status nyayo nyayo-worker --no-pager
systemctl list-timers nyayo-expire.timer nyayo-backup.timer --no-pager
echo
echo "Follow the log with: journalctl -u nyayo -f"
