#!/usr/bin/env bash
# Create a local .venv (Python >=3.10) and install QP Helper + RAG deps on macOS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib_macos.sh"

PYTHON="$(qp_pick_python || true)"
if [[ -z "$PYTHON" ]]; then
  echo "No Python >=3.10 found. Install via Homebrew, python.org, or micromamba/miniforge," >&2
  echo "or set PYTHON=/path/to/python3.11 before running this script." >&2
  echo "Apple CLT python3 is 3.9.x and cannot install lancedb>=0.34." >&2
  exit 1
fi

echo "Using $PYTHON ($("$PYTHON" --version 2>&1))"

# Recreate .venv if missing, <3.10, or broken (e.g. ensurepip never ran).
REUSE=0
if [[ -x "$ROOT/.venv/bin/python" ]] && qp_venv_meets_310 "$ROOT"; then
  if "$ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    REUSE=1
  fi
fi
if [[ "$REUSE" -eq 1 ]]; then
  echo "Reusing existing .venv ($("$ROOT/.venv/bin/python" --version 2>&1))"
else
  if [[ -d "$ROOT/.venv" ]]; then
    echo "Existing .venv unusable (<3.10 or no pip) — recreating…"
    rm -rf "$ROOT/.venv"
  fi
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [[ -f "$ROOT/requirements-rag.txt" ]]; then
  echo "Installing RAG backend deps (LanceDB / FastAPI)…"
  python -m pip install -r requirements-rag.txt
fi

if [[ ! -f "$ROOT/qp_email_helper.config.json" && -f "$ROOT/qp_email_helper.config.example.json" ]]; then
  cp "$ROOT/qp_email_helper.config.example.json" "$ROOT/qp_email_helper.config.json"
  echo "Created qp_email_helper.config.json from example (add API key in Settings)."
fi

echo
echo "Optional: install Playwright browsers for Outlook email fetch"
echo "  .venv/bin/playwright install chromium"
echo
echo "Next:"
echo "  ./scripts/run.sh                    # GUI (auto-starts RAG)"
echo "  ./scripts/run_rag_service.sh        # RAG alone"
echo "  ./scripts/smoke_macos.sh            # quick health check"
