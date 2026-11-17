"""Audit trail logger for QP Email Helper (reconstructed from bytecode)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_manager import APP_ROOT, get_actor

AUDIT_DIR = APP_ROOT / "config"
AUDIT_FILE = AUDIT_DIR / "audit_log.jsonl"


def log_action(
    action: str,
    query: str = "",
    g_paths: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append an audit entry.

    Args:
        action:   one of 'retrieve', 'ask', 'send_to_workbuddy', 'ingest',
                  'popup_triggered', 'popup_dismissed', 'popup_opened'
        query:    the user's question or search query
        g_paths:  list of G: drive paths involved
        metadata: any additional context (score, source_type, etc.)
    """
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": get_actor(),
        "action": action,
        "query": query,
        "g_paths": g_paths or [],
        "metadata": metadata or {},
    }
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_audit_log(limit: int = 100) -> list[dict[str, Any]]:
    """Read the last N audit entries (most recent first)."""
    if not AUDIT_FILE.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(AUDIT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(entries[-limit:]))
