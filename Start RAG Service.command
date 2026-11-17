#!/bin/bash
# Double-click launcher: start local RAG service with logs visible in Terminal.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || {
  echo "ERROR: cannot cd to $ROOT" >&2
  read -r -p "Press Enter to close…"
  exit 1
}

chmod +x "$ROOT/scripts/"*.sh "$ROOT/Start RAG Service.command" 2>/dev/null || true
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$ROOT/Start RAG Service.command" "$ROOT/scripts" 2>/dev/null || true
fi

# shellcheck disable=SC1091
if [[ -f "$ROOT/scripts/lib_macos.sh" ]]; then
  source "$ROOT/scripts/lib_macos.sh"
  qp_fix_launch_perms "$ROOT"
fi

echo "QP Helper — RAG service"
echo "Repo: $ROOT"
echo

if [[ ! -f "$ROOT/scripts/run_rag_service.sh" ]]; then
  echo "ERROR: scripts/run_rag_service.sh missing" >&2
  read -r -p "Press Enter to close…"
  exit 1
fi
chmod +x "$ROOT/scripts/run_rag_service.sh" 2>/dev/null || true

if ! "$ROOT/scripts/run_rag_service.sh"; then
  EC=$?
  echo
  echo "RAG service exited with code $EC."
  echo "See logs/rag_service.log if present."
  read -r -p "Press Enter to close…"
  exit "$EC"
fi
