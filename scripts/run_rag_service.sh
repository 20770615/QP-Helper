#!/usr/bin/env bash
# Start the local RAG FastAPI service (macOS / Linux) with visible logs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib_macos.sh"

if ! qp_venv_meets_310 "$ROOT"; then
  echo "No usable .venv (Python >=3.10). Running setup_macos.sh…"
  "$ROOT/scripts/setup_macos.sh"
fi

if [[ ! -f "$ROOT/rag_service/main.py" ]]; then
  echo "ERROR: rag_service/main.py missing from $ROOT" >&2
  exit 1
fi

# Ensure RAG Python deps are present (fastapi/lancedb are cheap sentinels).
if ! qp_rag_imports_ok "$ROOT"; then
  echo "RAG packages missing — installing via setup_rag_macos.sh…"
  "$ROOT/scripts/setup_rag_macos.sh"
fi

if [[ ! -f "$ROOT/qp_email_helper.config.json" && -f "$ROOT/qp_email_helper.config.example.json" ]]; then
  cp "$ROOT/qp_email_helper.config.example.json" "$ROOT/qp_email_helper.config.json"
  echo "Created qp_email_helper.config.json from example."
fi

export RAG_VECTOR_BACKEND="${RAG_VECTOR_BACKEND:-lancedb}"
export RAG_CONFIG_PATH="${RAG_CONFIG_PATH:-$ROOT/qp_email_helper.config.json}"
PORT="${RAG_SERVICE_PORT:-8765}"

mkdir -p "$ROOT/logs"
LOG_FILE="$ROOT/logs/rag_service.log"
echo "Starting rag_service on 127.0.0.1:${PORT} (backend=${RAG_VECTOR_BACKEND})"
echo "venv: $("$ROOT/.venv/bin/python" --version 2>&1)"
echo "Logs also append to $LOG_FILE"
echo

# Tee so Terminal (double-click .command) stays visible AND logs/rag_service.log
# gets a durable copy for RagServiceManager / debugging.
"$ROOT/.venv/bin/python" "$ROOT/rag_service/main.py" \
  --host 127.0.0.1 --port "$PORT" "$@" 2>&1 | tee -a "$LOG_FILE"
exit "${PIPESTATUS[0]}"
