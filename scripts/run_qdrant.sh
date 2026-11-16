#!/usr/bin/env bash
# Launch QP Helper with the disk-backed LanceDB vector store (macOS / Linux).
# Mirrors run_qdrant.bat on Windows. The in-app RAG manager then starts
# rag_service/main.py automatically; for a standalone service use
# ./scripts/run_rag_service.sh or double-click "Start RAG Service.command".
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -f "$ROOT/rag_service/main.py" ]]; then
  echo "ERROR: rag_service/main.py missing under $ROOT" >&2
  echo "Pull the branch that includes rag/ + rag_service/, then retry." >&2
  exit 1
fi
export RAG_VECTOR_BACKEND=lancedb
exec "$ROOT/scripts/run.sh" "$@"
