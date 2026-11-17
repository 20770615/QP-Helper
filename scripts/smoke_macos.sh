#!/usr/bin/env bash
# Lightweight Mac/Linux smoke checks for QP Helper + local RAG.
# Never prints API keys. Skips live LLM /ask when no key is configured.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib_macos.sh"

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

echo "QP Helper smoke (macos/linux)"
echo "Repo: $ROOT"
echo

# ── 1. venv Python version ──────────────────────────────────────────────────
if qp_venv_meets_310 "$ROOT"; then
  ok "venv Python $($ROOT/.venv/bin/python --version 2>&1)"
else
  bad "venv missing or Python <3.10 — run ./scripts/setup_macos.sh"
  echo
  echo "Summary: $PASS pass, $FAIL fail, $SKIP skip"
  exit 1
fi

# ── 2. model normalize (no network) ─────────────────────────────────────────
if "$ROOT/.venv/bin/python" - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path("src").resolve()))
from config_manager import normalize_llm_model
cases = {
    "deepseek-V4-Pro": "deepseek-v4-pro",
    "deepseek-V4-flash": "deepseek-flash",
    "DeepSeek_v4_pro": "deepseek-v4-pro",
    "": "",
}
for raw, expect in cases.items():
    got = normalize_llm_model(raw)
    if got != expect:
        raise SystemExit(f"{raw!r} -> {got!r}, expected {expect!r}")
print("normalize ok")
PY
then
  ok "model id normalize"
else
  bad "model id normalize"
fi

# ── 3. RAG imports ──────────────────────────────────────────────────────────
if qp_rag_imports_ok "$ROOT"; then
  ok "RAG imports (fastapi/lancedb/…)"
else
  bad "RAG imports — run ./scripts/setup_rag_macos.sh"
  echo
  echo "Summary: $PASS pass, $FAIL fail, $SKIP skip"
  exit 1
fi

# ── 4. /health (start service briefly if needed) ────────────────────────────
health_ok=0
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 2 "$BASE/health" >/dev/null 2>&1; then
    health_ok=1
  else
    echo "Starting temporary RAG service on :$PORT…"
    mkdir -p "$ROOT/logs"
    export RAG_VECTOR_BACKEND="${RAG_VECTOR_BACKEND:-lancedb}"
    export RAG_CONFIG_PATH="$CONFIG"
    "$ROOT/.venv/bin/python" "$ROOT/rag_service/main.py" \
      --host 127.0.0.1 --port "$PORT" >>"$ROOT/logs/smoke_rag.log" 2>&1 &
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
else
  skip "/health (curl not installed)"
fi

if [[ "$health_ok" -eq 1 ]]; then
  BODY="$(curl -fsS --max-time 2 "$BASE/health" || true)"
  ok "/health -> $BODY"
else
  bad "/health unreachable on $BASE (see logs/smoke_rag.log)"
fi

# ── 5. optional /ask dry-run (only if config has a key; never print it) ─────
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
key = (d.get("llm_api_key") or "").strip()
print(1 if key else 0)
PY
)"
fi

if [[ "$health_ok" -ne 1 ]]; then
  skip "/ask (no healthy RAG)"
elif [[ "$HAS_KEY" != "1" ]]; then
  skip "/ask (no llm_api_key in config — not inventing secrets)"
else
  # Push normalized model then ask a trivial ping. Failures are reported without keys.
  MODEL="$("$ROOT/.venv/bin/python" - <<PY
import json
from pathlib import Path
import sys
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
    bad "/ask HTTP $HTTP_CODE (expected 200; model casing / key / provider?)"
  elif "$ROOT/.venv/bin/python" - <<PY
import json
from pathlib import Path
d = json.loads(Path(r"""$ASK_OUT""").read_text(encoding="utf-8"))
ans = (d.get("answer") or "")[:120]
print(f"note={d.get('note')!s} answer={ans!r}")
if d.get("note") == "error" or d.get("error"):
    raise SystemExit(2)
PY
  then
    ok "/ask dry-run"
  else
    bad "/ask error payload (wrong model id / key / provider?)"
  fi
  rm -f "$ASK_OUT"
fi

echo
echo "Summary: $PASS pass, $FAIL fail, $SKIP skip"
[[ "$FAIL" -eq 0 ]]
