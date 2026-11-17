#!/usr/bin/env bash
# Launch QP Helper (macOS / Linux).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib_macos.sh"

if ! qp_venv_meets_310 "$ROOT"; then
  echo "No usable .venv (Python >=3.10). Run ./scripts/setup_macos.sh first." >&2
  exit 1
fi

export RAG_VECTOR_BACKEND="${RAG_VECTOR_BACKEND:-lancedb}"
exec "$ROOT/.venv/bin/python" "$ROOT/src/qp_email_helper.py" "$@"
