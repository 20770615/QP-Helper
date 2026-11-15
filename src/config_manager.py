"""Configuration manager for QP Email Helper (reconstructed from bytecode)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from platform_compat import default_g_paths, default_shared_root, is_macos

def _app_root() -> Path:
    """Resolve the application root directory.

    Works in both dev (script) and frozen (PyInstaller onedir / .app) modes.
    """
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
        # PyInstaller .app: Contents/MacOS/<exe> → prefer Contents/Resources if present,
        # otherwise walk up to the bundle's parent distribution folder.
        if is_macos():
            if root.name == "MacOS" and root.parent.name == "Contents":
                resources = root.parent / "Resources"
                if resources.is_dir():
                    root = resources
                else:
                    # Fall back to the folder that contains QP Helper.app
                    root = root.parent.parent.parent
    else:
        root = Path(__file__).resolve().parent
        # src / recovered_src sit beside the portable root when developed in-place;
        # prefer the portable package root if present.
        if root.name in {"recovered_src", "recovered_src_ui_beauty", "src"}:
            root = root.parent
        if root.name == "_internal":
            root = root.parent
    if root.name == "_internal":
        root = root.parent
    return root


APP_ROOT = _app_root()
CONFIG_DIR = APP_ROOT / "config"
USER_FILE = CONFIG_DIR / "user.json"
APP_CONFIG_FILE = APP_ROOT / "qp_email_helper.config.json"

# Platform-aware defaults: Windows keeps empty network-drive placeholders so
# teams still point at G:; macOS/Linux seed a local Documents shared folder.
DEFAULT_G_PATHS = default_g_paths()
DEFAULT_SHARED_ROOT = default_shared_root()
DEFAULT_UI_LANGUAGE = "zh"

DEFAULT_APP_CONFIG: dict[str, Any] = {
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    "auto_retrieve": True,
    "email_account": "",
    "rag_service_port": 8765,
    "popup_threshold": 0.75,
    "popup_cooldown_seconds": 120,
    "popup_enabled": True,
    "extractive_summary_enabled": True,
    "g_paths": dict(DEFAULT_G_PATHS),
    "shared_root": DEFAULT_SHARED_ROOT,
    "ui_language": DEFAULT_UI_LANGUAGE,
    "holdings_csv_path": "",
    "ms_client_id": "",
    "ms_tenant_id": "",
}

_LEGACY_LANG_MAP = {
    "中文": "zh",
    "English": "en",
    "zh-CN": "zh",
    "en-US": "en",
}


def get_display_name() -> str | None:
    """Return the saved display name, or None if not yet set."""
    if not USER_FILE.exists():
        return None
    try:
        data = json.loads(USER_FILE.read_text(encoding="utf-8"))
        return data.get("display_name")
    except Exception:
        return None


def set_display_name(name: str) -> None:
    """Save the display name (first-run onboarding)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USER_FILE.write_text(
        json.dumps({"display_name": (name or "").strip()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_actor() -> str:
    """Return the display name for audit tagging, or 'unknown'."""
    return get_display_name() or "unknown"


def load_app_config() -> dict[str, Any]:
    """Load the app config file, merging with defaults."""
    cfg = dict(DEFAULT_APP_CONFIG)
    if APP_CONFIG_FILE.exists():
        try:
            data = json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_app_config(cfg: dict[str, Any]) -> None:
    """Persist the app config (LLM key etc.) to disk atomically."""
    APP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = APP_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.replace(APP_CONFIG_FILE)
    except Exception:
        APP_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def update_app_config(**kwargs) -> dict[str, Any]:
    cfg = load_app_config()
    cfg.update(kwargs)
    save_app_config(cfg)
    return cfg


def get_llm_config() -> dict[str, Any]:
    cfg = load_app_config()
    return {
        "base_url": (cfg.get("llm_base_url") or "").strip(),
        "api_key": (cfg.get("llm_api_key") or "").strip(),
        "model": (cfg.get("llm_model") or "").strip(),
        "auto_retrieve": bool(cfg.get("auto_retrieve", True)),
    }


def get_rag_service_port() -> int:
    try:
        return int(load_app_config().get("rag_service_port") or 8765)
    except Exception:
        return 8765


def get_popup_config() -> dict[str, Any]:
    cfg = load_app_config()
    return {
        "threshold": float(cfg.get("popup_threshold") or 0.75),
        "cooldown_seconds": int(cfg.get("popup_cooldown_seconds") or 120),
        "enabled": bool(cfg.get("popup_enabled", True)),
        "extractive_summary_enabled": bool(cfg.get("extractive_summary_enabled", True)),
    }


def get_g_paths() -> dict[str, str]:
    cfg = load_app_config()
    paths = dict(DEFAULT_G_PATHS)
    raw = cfg.get("g_paths") or {}
    if isinstance(raw, dict):
        paths.update({k: str(v) for k, v in raw.items()})
    return paths


def get_language() -> str:
    cfg = load_app_config()
    lang = (cfg.get("ui_language") or DEFAULT_UI_LANGUAGE).strip()
    lang = _LEGACY_LANG_MAP.get(lang, lang)
    # also honor legacy config.json {"language": "中文"}
    legacy = APP_ROOT / "config.json"
    if legacy.exists():
        try:
            d = json.loads(legacy.read_text(encoding="utf-8"))
            if "ui_language" not in cfg or not cfg.get("ui_language"):
                lang = _LEGACY_LANG_MAP.get(d.get("language", lang), lang)
        except Exception:
            pass
    return "zh" if lang.startswith("zh") else "en"


def set_language(lang: str) -> None:
    lang = _LEGACY_LANG_MAP.get(lang, lang)
    lang = "zh" if str(lang).startswith("zh") else "en"
    update_app_config(ui_language=lang)


def get_shared_root() -> str:
    return (load_app_config().get("shared_root") or DEFAULT_SHARED_ROOT).strip()


def set_shared_root(path: str) -> None:
    update_app_config(shared_root=(path or "").strip())


def get_holdings_csv_path() -> str:
    cfg = load_app_config()
    h = (cfg.get("holdings_csv_path") or "").strip()
    if h and Path(h).exists():
        return h
    root = get_shared_root()
    cand = Path(root) / "ucits_fund_holdings.csv"
    return str(cand) if cand.exists() else h


def set_holdings_csv_path(path: str) -> None:
    update_app_config(holdings_csv_path=(path or "").strip())


def get_email_account() -> str:
    return (load_app_config().get("email_account") or "").strip()


def set_email_account(account: str) -> None:
    update_app_config(email_account=(account or "").strip())
