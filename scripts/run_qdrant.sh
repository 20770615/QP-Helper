#!/usr/bin/env bash
# Launch QP Helper with the disk-backed LanceDB vector store (macOS / Linux).
# Mirrors run_qdrant.bat on Windows.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export RAG_VECTOR_BACKEND=lancedb
exec "$ROOT/scripts/run.sh" "$@"
