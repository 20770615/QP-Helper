#!/bin/bash
# Double-click launcher for macOS (opens in Terminal.app).
# Keeps the Terminal window open if setup or launch fails so errors stay visible.
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || {
  echo "ERROR: cannot cd to repo root: $ROOT" >&2
  read -r -p "Press Enter to close…"
  exit 1
}

# shellcheck disable=SC1091
source "$ROOT/scripts/lib_macos.sh"

echo "QP Helper"
echo "Repo: $ROOT"
echo

NEED_SETUP=0
if ! qp_venv_meets_310 "$ROOT"; then
  NEED_SETUP=1
elif ! qp_rag_imports_ok "$ROOT"; then
  NEED_SETUP=1
fi

if [[ "$NEED_SETUP" -eq 1 ]]; then
  echo "Setting up .venv / deps (first launch may take a few minutes)…"
  if [[ ! -x "$ROOT/scripts/setup_macos.sh" ]]; then
    echo "ERROR: scripts/setup_macos.sh is missing." >&2
    read -r -p "Press Enter to close…"
    exit 1
  fi
  if ! "$ROOT/scripts/setup_macos.sh"; then
    echo "ERROR: setup_macos.sh failed." >&2
    read -r -p "Press Enter to close…"
    exit 1
  fi
fi

if [[ ! -f "$ROOT/qp_email_helper.config.json" ]]; then
  if [[ -f "$ROOT/qp_email_helper.config.example.json" ]]; then
    cp "$ROOT/qp_email_helper.config.example.json" "$ROOT/qp_email_helper.config.json"
    echo "Created qp_email_helper.config.json from example (fill in API key in Settings)."
  else
    echo "WARNING: config example missing; continuing without local config file."
  fi
fi

export RAG_VECTOR_BACKEND="${RAG_VECTOR_BACKEND:-lancedb}"

echo "Launching QP Helper…"
echo "(You can minimize this Terminal — closing it may quit the app.)"
echo

# Do not exec: we want to catch non-zero exits and pause.
if "$ROOT/scripts/run.sh"; then
  exit 0
fi

EC=$?
echo
echo "QP Helper exited with code $EC."
echo "If the window vanished immediately, scroll up for Python/Qt errors."
read -r -p "Press Enter to close…"
exit "$EC"
