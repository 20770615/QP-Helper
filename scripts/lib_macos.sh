#!/usr/bin/env bash
# Shared helpers for macOS / Linux setup + smoke scripts.
# shellcheck shell=bash

# Prefer PYTHON=… if set; else any python3.10+ on PATH / common user-local installs.
# Apple CLT ships 3.9.6 — too old for lancedb>=0.34.
qp_pick_python() {
  local cand ver major minor
  for cand in \
    "${PYTHON:-}" \
    "$HOME/.local/micromamba/envs/qp-helper-py311/bin/python" \
    "$HOME/.local/micromamba/envs/qp-helper-py311/bin/python3" \
    "$HOME/miniforge3/bin/python3" \
    "$HOME/mambaforge/bin/python3" \
    "$HOME/miniconda3/bin/python3" \
    "$HOME/anaconda3/bin/python3" \
    "/opt/homebrew/bin/python3.12" \
    "/opt/homebrew/bin/python3.11" \
    "/opt/homebrew/bin/python3.10" \
    "/usr/local/bin/python3.12" \
    "/usr/local/bin/python3.11" \
    "/usr/local/bin/python3.10" \
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

qp_venv_python() {
  local root="${1:-.}"
  echo "$root/.venv/bin/python"
}

qp_venv_meets_310() {
  local py
  py="$(qp_venv_python "${1:-.}")"
  [[ -x "$py" ]] || return 1
  "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

qp_require_venv_310() {
  local root="${1:-.}"
  if ! qp_venv_meets_310 "$root"; then
    echo "ERROR: .venv needs Python >=3.10 (Apple CLT python3 is 3.9.x)." >&2
    echo "Run: ./scripts/setup_macos.sh" >&2
    echo "Or:  PYTHON=/path/to/python3.11 ./scripts/setup_macos.sh" >&2
    return 1
  fi
  return 0
}

qp_rag_imports_ok() {
  local py
  py="$(qp_venv_python "${1:-.}")"
  [[ -x "$py" ]] || return 1
  "$py" -c "import fastapi, uvicorn, lancedb, sentence_transformers, httpx" 2>/dev/null
}

# After git clone / zip extract, +x bits or Gatekeeper quarantine can break
# double-click launchers. Best-effort restore; never fails the caller.
qp_fix_launch_perms() {
  local root="${1:-.}"
  chmod +x \
    "$root/scripts/"*.sh \
    "$root/Start QP Helper.command" \
    "$root/Start RAG Service.command" \
    "$root/Start QP Helper.app/Contents/MacOS/launcher" \
    2>/dev/null || true
  if command -v xattr >/dev/null 2>&1; then
    xattr -dr com.apple.quarantine \
      "$root/Start QP Helper.command" \
      "$root/Start RAG Service.command" \
      "$root/Start QP Helper.app" \
      "$root/scripts" \
      2>/dev/null || true
  fi
}
