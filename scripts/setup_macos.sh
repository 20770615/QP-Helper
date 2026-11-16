#!/usr/bin/env bash
# Create a local .venv and install QP Helper deps on macOS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Prefer PYTHON=… if set; else any python3.10+ on PATH / common user-local installs.
# Apple CLT ships 3.9.6 — too old for lancedb>=0.34. Micromamba/miniforge/python.org
# installs under ~/.local are checked before falling back to system python3.
_pick_python() {
  local cand ver major minor
  for cand in \
    "${PYTHON:-}" \
    "$HOME/.local/micromamba/envs/qp-helper-py311/bin/python" \
    "$HOME/.local/micromamba/envs/qp-helper-py311/bin/python3" \
    "$HOME/miniforge3/bin/python3" \
    "$HOME/mambaforge/bin/python3" \
    "$HOME/.local/bin/python3.12" \
    "$HOME/.local/bin/python3.11" \
    "$HOME/.local/bin/python3.10" \
    "$(command -v python3.12 2>/dev/null || true)" \
    "$(command -v python3.11 2>/dev/null || true)" \
    "$(command -v python3.10 2>/dev/null || true)" \
    "$(command -v python3 2>/dev/null || true)"
  do
    [[ -n "$cand" && -x "$cand" ]] || continue
    ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    [[ -n "$ver" ]] || continue
    major="${ver%%.*}"
    minor="${ver#*.}"
    if [[ "$major" -gt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -ge 10 ]]; }; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

PYTHON="$(_pick_python || true)"
if [[ -z "$PYTHON" ]]; then
  echo "No Python >=3.10 found. Install via micromamba/miniforge or python.org," >&2
  echo "or set PYTHON=/path/to/python3.11 before running this script." >&2
  echo "Apple CLT python3 is 3.9.x and cannot install lancedb>=0.34." >&2
  exit 1
fi

echo "Using $PYTHON ($("$PYTHON" --version 2>&1))"
"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [[ -f "$ROOT/requirements-rag.txt" ]]; then
  echo "Installing RAG backend deps (LanceDB / FastAPI)…"
  python -m pip install -r requirements-rag.txt
fi

echo
echo "Optional: install Playwright browsers for Outlook email fetch"
echo "  .venv/bin/playwright install chromium"
echo
echo "Next:"
echo "  cp qp_email_helper.config.example.json qp_email_helper.config.json"
echo "  ./scripts/run.sh"
echo "  # or start RAG alone: ./scripts/run_rag_service.sh"
