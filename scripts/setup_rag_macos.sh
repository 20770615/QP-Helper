#!/usr/bin/env bash
# Install RAG backend deps into the local .venv (macOS / Linux).
# Needs Python >=3.10 for lancedb>=0.34 (Apple CLT 3.9 is too old).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib_macos.sh"

if ! qp_venv_meets_310 "$ROOT"; then
  echo "No usable .venv (Python >=3.10) — running scripts/setup_macos.sh…"
  "$ROOT/scripts/setup_macos.sh"
fi

PYVER="$("$ROOT/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "venv Python $PYVER ($("$ROOT/.venv/bin/python" --version 2>&1))"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements-rag.txt

if ! qp_rag_imports_ok "$ROOT"; then
  echo "ERROR: RAG imports still failing after install." >&2
  exit 1
fi

echo
echo "RAG deps installed."
echo "Start the service with:"
echo "  ./scripts/run_rag_service.sh"
echo "or launch the app (auto-starts RAG on launch)."
