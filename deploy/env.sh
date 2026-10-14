# Sourced by the other deploy scripts: finds the repository root and the nyayo conda env's bin directory.
# Override the env with NYAYO_CONDA_ENV=<name> or point straight at it with NYAYO_BIN=/path/to/env/bin.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${NYAYO_BIN:-}" ]; then
    if ! command -v conda >/dev/null 2>&1; then
        echo "conda not found on PATH; set NYAYO_BIN to the env's bin directory" >&2
        exit 1
    fi
    NYAYO_BIN="$(conda info --base)/envs/${NYAYO_CONDA_ENV:-nyayo}/bin"
fi

if [ ! -x "$NYAYO_BIN/python" ]; then
    echo "no python in $NYAYO_BIN; create the env first (see docs/deploy.md)" >&2
    exit 1
fi
