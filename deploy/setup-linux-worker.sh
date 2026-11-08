#!/usr/bin/env bash
# Sets this Linux machine up as a nyayo worker (Fedora and RHEL-likes with dnf, Ubuntu and Debian with apt).
#
# It installs what a worker needs if it is missing (git, make, a C++ compiler, python3 with venv, curl, unzip; with sudo),
# downloads the client from nyayo and checks its sha256, makes a venv, and writes run.sh and worker.env, which start it.
# Run it again at any time: what is already there is kept, worker.token included.
#
#   curl -fsSL http://SERVER/setup-linux-worker.sh | bash -s -- --service
#   bash setup-linux-worker.sh --name hunt --threads 16 --join ABCD-1234 --service
#
# Options: --server URL  --name NAME  --threads N  --dir DIR  --join CODE  --service  --no-install  -h
#
# The worker is pending until approved on nyayo's Lab, Machines page, unless it has a join code made there.
set -euo pipefail

SERVER='http://172.16.100.11:8001'
NAME="$(hostname -s 2>/dev/null || hostname)"
THREADS=0
DIR="$HOME/nyayo-worker"
JOIN=''
SERVICE=0
INSTALL=1

die()  { printf '\n%s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --server)     [ $# -ge 2 ] || die "--server needs a value"; SERVER="$2"; shift 2 ;;
    --name)       [ $# -ge 2 ] || die "--name needs a value"; NAME="$2"; shift 2 ;;
    --threads)    [ $# -ge 2 ] || die "--threads needs a value"; THREADS="$2"; shift 2 ;;
    --dir)        [ $# -ge 2 ] || die "--dir needs a value"; DIR="$2"; shift 2 ;;
    --join)       [ $# -ge 2 ] || die "--join needs a value"; JOIN="$2"; shift 2 ;;
    --service)    SERVICE=1; shift ;;
    --no-install) INSTALL=0; shift ;;
    -h|--help)    echo "Usage: setup-linux-worker.sh [--server URL] [--name NAME] [--threads N] [--dir DIR] [--join CODE] [--service] [--no-install]"; exit 0 ;;
    *)            die "Unknown option $1 (try -h)" ;;
  esac
done
SERVER="${SERVER%/}"
case "$THREADS" in ''|*[!0-9]*) die "--threads must be a number" ;; esac
case "$DIR" in /*) ;; *) DIR="$PWD/$DIR" ;; esac
case "$JOIN$NAME" in *\'*|*\"*|*\ *|*\$*|*\`*|*\\*) die "--join and --name can't hold quotes, spaces or shell characters." ;; esac

# ----------------------------------------------------------------------------------------------------------------------
step "nyayo at $SERVER"
command -v curl >/dev/null 2>&1 || [ "$INSTALL" -eq 1 ] || die "curl is missing."
ref="$(curl -fsS --max-time 15 -X POST "$SERVER/clientVersionRef/")" ||
  die "Can't reach $SERVER/clientVersionRef/. Is this machine on the same network as bobby?"

# ----------------------------------------------------------------------------------------------------------------------
step "Distribution"
[ -r /etc/os-release ] || die "No /etc/os-release: can't tell which distribution this is."
# os-release also sets NAME, which is ours: read the three fields we need out of it instead of sourcing it
osr() { sed -n "s/^$1=//p" /etc/os-release | head -n1 | tr -d '"'"'"; }
ID="$(osr ID)"; ID_LIKE="$(osr ID_LIKE)"; PRETTY_NAME="$(osr PRETTY_NAME)"
like=" ${ID} ${ID_LIKE} "
case "$like" in
  *" fedora "*|*" rhel "*|*" centos "*) FAMILY=dnf; PKGS=(git make gcc-c++ python3 curl unzip) ;;
  *" ubuntu "*|*" debian "*)            FAMILY=apt; PKGS=(git make g++ python3 python3-venv curl unzip) ;;
  *) die "This script knows Fedora (and RHEL-likes, with dnf) and Ubuntu or Debian (with apt), not ${PRETTY_NAME:-${ID:-this system}}." ;;
esac
ok "${PRETTY_NAME:-$ID} ($FAMILY)"

SUDO=''
[ "$(id -u)" -eq 0 ] || SUDO=sudo

# ----------------------------------------------------------------------------------------------------------------------
step "Packages"
missing=()
if [ "$FAMILY" = dnf ]; then
  for p in "${PKGS[@]}"; do rpm -q "$p" >/dev/null 2>&1 || missing+=("$p"); done
else
  for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
fi
if [ ${#missing[@]} -eq 0 ]; then
  ok "all there: ${PKGS[*]}"
elif [ "$INSTALL" -eq 0 ]; then
  die "Missing: ${missing[*]}. Run without --no-install to install them, or install them yourself."
else
  if [ -n "$SUDO" ] && ! command -v sudo >/dev/null 2>&1; then die "Missing ${missing[*]}, and sudo isn't here to install it."; fi
  ok "installing ${missing[*]}"
  if [ "$FAMILY" = dnf ]; then
    $SUDO dnf install -y "${missing[@]}"
  else
    $SUDO apt-get update
    $SUDO apt-get install -y "${missing[@]}"
  fi
fi

# ----------------------------------------------------------------------------------------------------------------------
step "The client, in $DIR"
mkdir -p "$DIR/client"
json() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
version="$(printf '%s' "$ref" | json client_version)"
if [ -f "$DIR/client/client.py" ]; then
  ok "already there; it updates itself from nyayo"
else
  url="$(printf '%s' "$ref" | json client_url)"
  want="$(printf '%s' "$ref" | json client_sha256)"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/client.zip" "$url"
  got="$(sha256sum "$tmp/client.zip" | cut -d' ' -f1)"
  [ "$got" = "$want" ] || die "client.zip's sha256 is $got, nyayo says $want."
  unzip -qo "$tmp/client.zip" -d "$DIR/client"
  ok "client $version, sha256 checked"
fi

# ----------------------------------------------------------------------------------------------------------------------
step "The venv"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$DIR/client/requirements.txt"
ok "requirements installed"

# ----------------------------------------------------------------------------------------------------------------------
step "run.sh and worker.env"
lscpu_field() { lscpu 2>/dev/null | awk -F: -v k="$1" '$1 == k {gsub(/ /, "", $2); print $2}'; }
cps="$(lscpu_field 'Core(s) per socket')"
SOCKETS="$(lscpu_field 'Socket(s)')"
SOCKETS="${SOCKETS:-1}"
if [ "$THREADS" -le 0 ]; then
  # physical cores: cores per socket times sockets, not hyperthreads
  THREADS=$(( ${cps:-0} * SOCKETS ))
  [ "$THREADS" -gt 0 ] || THREADS="$(nproc)"
fi

cat > "$DIR/worker.env" <<ENV
SERVER=$SERVER
THREADS=$THREADS
SOCKETS=$SOCKETS
NAME=$NAME
ENV

cat > "$DIR/run.sh" <<'RUN'
#!/usr/bin/env bash
# Starts this machine's nyayo worker. Written by setup-linux-worker.sh; its settings are in worker.env.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/worker.env"
JOIN_ARG=()
RUN
if [ -n "$JOIN" ]; then
  # the code is for the first start only: with a worker.token there is no --join
  printf '%s\n' "[ -f \"\$here/client/worker.token\" ] || JOIN_ARG=(--join '$JOIN')" >> "$DIR/run.sh"
fi
cat >> "$DIR/run.sh" <<'RUN'
cd "$here/client"
exec "$here/venv/bin/python" client.py --server "$SERVER" -T "$THREADS" -N "$SOCKETS" -I "$NAME" "${JOIN_ARG[@]}"
RUN
chmod +x "$DIR/run.sh"
ok "$NAME, $THREADS threads, $SOCKETS socket(s)"

# ----------------------------------------------------------------------------------------------------------------------
if [ "$SERVICE" -eq 1 ]; then
  step "systemd service nyayo-client"
  user="$(id -un)"
  # Through /bin/bash, not the script itself: that is what SELinux lets a service run from a home directory.
  printf '%s\n' \
    '[Unit]' \
    'Description=nyayo worker' \
    'After=network-online.target' \
    'Wants=network-online.target' \
    '' \
    '[Service]' \
    "User=$user" \
    'Environment=PYTHONUNBUFFERED=1' \
    "ExecStart=/bin/bash $DIR/run.sh" \
    'Restart=always' \
    'RestartSec=30' \
    '' \
    '[Install]' \
    'WantedBy=multi-user.target' |
    $SUDO tee /etc/systemd/system/nyayo-client.service >/dev/null
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now nyayo-client
  ok "nyayo-client.service is enabled and started"
  echo "   Follow it:  journalctl -u nyayo-client -f"
else
  step "Done"
  echo "   Start it:  $DIR/run.sh"
fi
[ -n "$JOIN" ] || echo "   Then approve '$NAME' on $SERVER under Lab, Machines."
