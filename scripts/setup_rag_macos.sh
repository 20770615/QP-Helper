#!/usr/bin/env bash
# Install RAG backend deps into the local .venv (macOS / Linux).
# Needs Python >=3.10 for lancedb>=0.34 (Apple CLT 3.9 is too old).
# Prefer: PYTHON=/path/to/python3.11 ./scripts/setup_macos.sh
# Or install micromamba + create env qp-helper-py311 (setup_macos.sh auto-detects).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "No .venv — running scripts/setup_macos.sh first…"
  "$ROOT/scripts/setup_macos.sh"
fi

PYVER="$("$ROOT/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "venv Python $PYVER ($("$ROOT/.venv/bin/python" --version 2>&1))"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements-rag.txt

echo
echo "RAG deps installed."
echo "Start the service with:"
echo "  ./scripts/run_rag_service.sh"
echo "or launch the app (Settings → Start service / auto-start)."
