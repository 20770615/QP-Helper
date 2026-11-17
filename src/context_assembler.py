"""Context assembler: Send to WorkBuddy payload builder (reconstructed)."""
from __future__ import annotations

import json
from typing import Any

from audit_logger import log_action
from config_manager import get_actor


def build_payload(
    question: str,
    g_paths: list[str] | None = None,
    fund_template: bool = False,
    extra_note: str = "",
) -> dict[str, Any]:
    """Build the send-to-workbuddy payload."""
    payload: dict[str, Any] = {
        "actor": get_actor(),
        "question": question or "",
        "kb_only": True,
        "g_paths": list(g_paths or []),
        "constraint": "all conclusions must cite chunk_id; if KB lacks data, state explicitly",
    }
    if fund_template:
        payload["fund_template_ref"] = (
            "9-step fund rebalancing reasoning template (see harness_thickness_spec.md)"
        )
    if extra_note:
        payload["note"] = extra_note
    return payload


def payload_to_text(payload: dict[str, Any]) -> str:
    """Render the payload as structured text for clipboard copying."""
    lines = [
        "============================================================",
        "  QP Email Helper -> WorkBuddy Context Package",
        "============================================================",
        "",
        f"Actor:        {payload.get('actor') or 'unknown'}",
        f"Question:     {payload.get('question') or ''}",
        f"KB-only:      {payload.get('kb_only')}",
        "",
        "G: Drive Documents:",
    ]
    for i, path in enumerate(payload.get("g_paths") or [], 1):
        lines.append(f"  [{i}] {path}")
    if payload.get("fund_template_ref"):
        lines.append(f"Fund Template: {payload['fund_template_ref']}")
    if payload.get("note"):
        lines.append(f"Note: {payload['note']}")
    if payload.get("constraint"):
        lines.append(f"Constraint: {payload['constraint']}")
    lines.append("")
    lines.append("JSON:")
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def payload_to_json_str(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def send_to_workbuddy(
    question: str,
    g_paths: list[str] | None = None,
    fund_template: bool = False,
    extra_note: str = "",
) -> str:
    """Assemble payload, audit-log, return clipboard text."""
    payload = build_payload(question, g_paths, fund_template, extra_note)
    log_action("send_to_workbuddy", query=question, g_paths=g_paths or [], metadata={"fund_template": fund_template})
    return payload_to_text(payload)


def try_invoke_workbuddy_cli(payload: dict[str, Any]) -> bool:
    """Future hook: invoke WorkBuddy CLI/URL scheme if available."""
    return False
