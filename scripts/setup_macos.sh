#!/usr/bin/env bash
# Create a local .venv and install QP Helper deps on macOS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  else
    echo "python3 not found. Install Python 3.11+ from python.org or Homebrew." >&2
    exit 1
  fi
fi

echo "Using $PYTHON"
"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Optional: install Playwright browsers for Outlook email fetch"
echo "  .venv/bin/playwright install chromium"
echo
echo "Next:"
echo "  cp qp_email_helper.config.example.json qp_email_helper.config.json"
echo "  ./scripts/run.sh"
