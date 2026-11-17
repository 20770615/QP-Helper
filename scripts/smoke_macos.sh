#!/usr/bin/env bash
# Smoke checks for QP Helper (macOS + Linux/CI).
#
# Default: offline — venv, imports, model normalize, rag module load,
# FastAPI TestClient /health+/status. No secrets invented; no live LLM.
#
# Env / flags:
#   SMOKE_SETUP=1 | --setup   run setup_macos.sh if .venv unusable
#   LIVE=1        | --live    also start (or reuse) uvicorn and curl /health
#                             and optional /ask when llm_api_key is set
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib_macos.sh"

LIVE=0
DO_SETUP=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    --setup) DO_SETUP=1 ;;
    -h|--help)
      echo "Usage: $0 [--setup] [--live]"
      exit 0
      ;;
  esac
done
[[ "${LIVE:-0}" == "1" ]] && LIVE=1
[[ "${SMOKE_SETUP:-0}" == "1" ]] && DO_SETUP=1

PORT="${RAG_SERVICE_PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
CONFIG="${RAG_CONFIG_PATH:-$ROOT/qp_email_helper.config.json}"
STARTED_RAG=0
RAG_PID=""
PASS=0
FAIL=0
SKIP=0

ok() { echo "PASS  $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }
skip() { echo "SKIP  $*"; SKIP=$((SKIP + 1)); }

cleanup() {
  if [[ "$STARTED_RAG" -eq 1 && -n "$RAG_PID" ]]; then
    kill "$RAG_PID" 2>/dev/null || true
    wait "$RAG_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ "$LIVE" -eq 1 ]]; then
  echo "QP Helper smoke (offline + live)"
else
  echo "QP Helper smoke (offline)"
fi
echo "Repo: $ROOT"
echo

# ── 0. required launch scripts present ─────────────────────────────────────
MISSING=0
for rel in \
  scripts/setup_macos.sh \
  scripts/setup_rag_macos.sh \
  scripts/run.sh \
  scripts/run_rag_service.sh \
  scripts/lib_macos.sh \
  "Start QP Helper.command" \
  "Start RAG Service.command" \
  rag_service/main.py \
  src/qp_email_helper.py \
  qp_email_helper.config.example.json
do
  if [[ ! -e "$ROOT/$rel" ]]; then
    bad "missing $rel"
    MISSING=1
  fi
done
[[ "$MISSING" -eq 0 ]] && ok "fresh-clone launch files present"

# ── 1. venv (optional auto-setup) ───────────────────────────────────────────
if ! qp_venv_meets_310 "$ROOT"; then
  if [[ "$DO_SETUP" -eq 1 ]]; then
    echo "Running setup_macos.sh…"
    "$ROOT/scripts/setup_macos.sh"
  else
    bad "venv missing or Python <3.10 — run ./scripts/setup_macos.sh (or $0 --setup)"
    echo
    echo "Summary: $PASS pass, $FAIL fail, $SKIP skip"
    exit 1
  fi
fi
ok "venv Python $("$ROOT/.venv/bin/python" --version 2>&1)"

# ── 2. model normalize (src + harness mirror) ───────────────────────────────
if "$ROOT/.venv/bin/python" - <<'PY'
import sys
from pathlib import Path
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT / "src"))
from config_manager import normalize_llm_model, DEFAULT_APP_CONFIG

cases = {
    "deepseek-V4-Pro": "deepseek-v4-pro",
    "deepseek-V4-flash": "deepseek-flash",
    "DeepSeek_v4_pro": "deepseek-v4-pro",
    "": "",
}
for raw, expect in cases.items():
    got = normalize_llm_model(raw)
    if got != expect:
        raise SystemExit(f"config_manager: {raw!r} -> {got!r}, expected {expect!r}")

# Defaults used by fresh config copy
if DEFAULT_APP_CONFIG.get("llm_model") != "deepseek-v4-pro":
    raise SystemExit(f"bad default llm_model: {DEFAULT_APP_CONFIG.get('llm_model')!r}")
if "deepseek.com" not in (DEFAULT_APP_CONFIG.get("llm_base_url") or ""):
    raise SystemExit(f"bad default llm_base_url: {DEFAULT_APP_CONFIG.get('llm_base_url')!r}")

# harness mirror (load as package module so dataclasses can resolve)
sys.path.insert(0, str(ROOT / "rag_service"))
from harness import OpenAIAgentsHarness  # noqa: E402
for raw, expect in (
    ("deepseek-V4-Pro", "deepseek-v4-pro"),
    ("deepseek-V4-flash", "deepseek-flash"),
):
    got = OpenAIAgentsHarness._normalize_model(raw)
    if got != expect:
        raise SystemExit(f"harness: {raw!r} -> {got!r}, expected {expect!r}")
print("normalize ok")
PY
then
  ok "model id normalize (config + harness)"
else
  bad "model id normalize"
fi

# ── 3. package imports / module load ────────────────────────────────────────
if "$ROOT/.venv/bin/python" - <<'PY'
import importlib
import sys
from pathlib import Path
ROOT = Path(".").resolve()

# GUI deps (import only — no QApplication)
from PyQt6.QtCore import QT_VERSION_STR  # noqa: F401
import httpx  # noqa: F401

# RAG stack
for name in ("fastapi", "uvicorn", "lancedb", "sentence_transformers", "openai"):
    importlib.import_module(name)

sys.path.insert(0, str(ROOT))
import rag  # noqa: F401
import rag.config
import rag.retrieve
import rag.store
assert rag.config.VECTOR_BACKEND  # env may override; just ensure attribute exists
print("imports ok", rag.config.VECTOR_BACKEND)
PY
then
  ok "PyQt6 + rag module load"
else
  bad "PyQt6 / rag module load — run ./scripts/setup_macos.sh"
  echo
  echo "Summary: $PASS pass, $FAIL fail, $SKIP skip"
  exit 1
fi

# ── 4. health handlers via FastAPI TestClient (no live port / no LLM) ───────
if "$ROOT/.venv/bin/python" - <<'PY'
import importlib.util
import sys
from pathlib import Path
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rag_service"))
spec = importlib.util.spec_from_file_location("rag_service_main", ROOT / "rag_service" / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from fastapi.testclient import TestClient
c = TestClient(mod.app)
h = c.get("/health")
assert h.status_code == 200 and h.json().get("status") == "ok", h.text
s = c.get("/status")
assert s.status_code == 200 and "status" in s.json(), s.text
# /config round-trip without secrets
r = c.post("/config", json={"model": "deepseek-V4-Pro"})
assert r.status_code == 200, r.text
body = r.json()
cfg = body.get("cfg") or {}
assert cfg.get("model") == "deepseek-v4-pro", body
print("handlers ok", h.json(), "status=", s.json().get("status"), "model=", cfg.get("model"))
PY
then
  ok "RAG /health + /status + /config normalize (TestClient)"
else
  bad "RAG health handlers (TestClient)"
fi

# ── 5. optional live curl /health + /ask ────────────────────────────────────
if [[ "$LIVE" -ne 1 ]]; then
  skip "live /health+/ask (pass --live to enable)"
else
  health_ok=0
  if ! command -v curl >/dev/null 2>&1; then
    skip "live /health (curl not installed)"
  else
    if curl -fsS --max-time 2 "$BASE/health" >/dev/null 2>&1; then
      health_ok=1
    else
      echo "Starting temporary RAG service on :$PORT…"
      mkdir -p "$ROOT/logs"
      export RAG_VECTOR_BACKEND="${RAG_VECTOR_BACKEND:-lancedb}"
      export RAG_CONFIG_PATH="$CONFIG"
      "$ROOT/.venv/bin/python" "$ROOT/rag_service/main.py" \
        --host 127.0.0.1 --port "$PORT" --no-auto-sync \
        >>"$ROOT/logs/smoke_rag.log" 2>&1 &
      RAG_PID=$!
      STARTED_RAG=1
      for _ in $(seq 1 40); do
        if curl -fsS --max-time 1 "$BASE/health" >/dev/null 2>&1; then
          health_ok=1
          break
        fi
        if ! kill -0 "$RAG_PID" 2>/dev/null; then
          break
        fi
        sleep 0.5
      done
    fi
    if [[ "$health_ok" -eq 1 ]]; then
      ok "live /health -> $(curl -fsS --max-time 2 "$BASE/health")"
    else
      bad "live /health unreachable (see logs/smoke_rag.log)"
    fi
  fi

  HAS_KEY=0
  if [[ -f "$CONFIG" ]]; then
    HAS_KEY="$("$ROOT/.venv/bin/python" - <<PY
import json
from pathlib import Path
p = Path(r"""$CONFIG""")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print(0); raise SystemExit
print(1 if (d.get("llm_api_key") or "").strip() else 0)
PY
)"
  fi

  if [[ "$health_ok" -ne 1 ]]; then
    skip "live /ask (no healthy RAG)"
  elif [[ "$HAS_KEY" != "1" ]]; then
    skip "live /ask (no llm_api_key — not inventing secrets)"
  else
    MODEL="$("$ROOT/.venv/bin/python" - <<PY
import json, sys
from pathlib import Path
sys.path.insert(0, "src")
from config_manager import normalize_llm_model
d = json.loads(Path(r"""$CONFIG""").read_text(encoding="utf-8"))
print(normalize_llm_model(d.get("llm_model")) or "deepseek-v4-pro")
PY
)"
    curl -fsS --max-time 5 -X POST "$BASE/config" \
      -H 'Content-Type: application/json' \
      -d "{\"model\":\"$MODEL\"}" >/dev/null || true
    ASK_OUT="$(mktemp)"
    HTTP_CODE="$(curl -sS --max-time 120 -o "$ASK_OUT" -w '%{http_code}' \
      -X POST "$BASE/ask" \
      -H 'Content-Type: application/json' \
      -d '{"query":"ping — reply with the single word pong","n":1,"force_retrieve":false}' \
      || true)"
    if [[ "$HTTP_CODE" != "200" ]]; then
      bad "live /ask HTTP $HTTP_CODE"
    elif "$ROOT/.venv/bin/python" - <<PY
import json
from pathlib import Path
d = json.loads(Path(r"""$ASK_OUT""").read_text(encoding="utf-8"))
print(f"note={d.get('note')!s} answer={(d.get('answer') or '')[:80]!r}")
if d.get("note") == "error" or d.get("error"):
    raise SystemExit(2)
PY
    then
      ok "live /ask dry-run"
    else
      bad "live /ask error payload"
    fi
    rm -f "$ASK_OUT"
  fi
fi

echo
echo "Summary: $PASS pass, $FAIL fail, $SKIP skip"
[[ "$FAIL" -eq 0 ]]
