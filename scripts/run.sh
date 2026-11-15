#!/usr/bin/env bash
# Launch QP Helper (macOS / Linux).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "No Python found. Run ./scripts/setup_macos.sh first." >&2
  exit 1
fi

export RAG_VECTOR_BACKEND="${RAG_VECTOR_BACKEND:-lancedb}"
exec "$PY" "$ROOT/src/qp_email_helper.py" "$@"
