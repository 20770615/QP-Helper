#!/bin/bash
# Double-click launcher: start local RAG service with logs visible in Terminal.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || {
  echo "ERROR: cannot cd to $ROOT" >&2
  read -r -p "Press Enter to close…"
  exit 1
}

echo "QP Helper — RAG service"
echo "Repo: $ROOT"
echo

if [[ ! -x "$ROOT/scripts/run_rag_service.sh" ]]; then
  echo "ERROR: scripts/run_rag_service.sh missing" >&2
  read -r -p "Press Enter to close…"
  exit 1
fi

if ! "$ROOT/scripts/run_rag_service.sh"; then
  EC=$?
  echo
  echo "RAG service exited with code $EC."
  echo "See logs/rag_service.log if present."
  read -r -p "Press Enter to close…"
  exit "$EC"
fi
