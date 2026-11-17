"""Local metadata index cache for G: drive documents.

Caches document METADATA only (not content -- no local mirror, per prompt 2
section 9 red line). This allows fast document listing and filtering without
hitting the network drive on every operation.

Schema (prompt 2, section 1):
    { doc_id, g_path, source_type, title, date, owner, tags, checksum }
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from config_manager import APP_ROOT

CACHE_DIR = APP_ROOT / "config"
CACHE_FILE = CACHE_DIR / "metadata_cache.json"


def _ensure_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _doc_id(g_path: str) -> str:
    """Deterministic doc_id from the G: path."""
    return hashlib.sha256(g_path.encode("utf-8")).hexdigest()[:16]


def _checksum(path: Path) -> str:
    """Quick file checksum (first 64KB + size) for change detection."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(65536)
        return f"{size}:{hashlib.md5(head).hexdigest()}"
    except Exception:
        return "unknown"


def load_cache() -> dict[str, dict[str, Any]]:
    """Load the metadata cache. Returns {doc_id: metadata_dict}."""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict[str, dict[str, Any]]) -> None:
    """Persist the metadata cache."""
    _ensure_dir()
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_or_update(
    g_path: str,
    source_type: str = "upload",
    title: str = "",
    date: str = "",
    owner: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Add or update a document in the metadata cache.

    Recomputes checksum to detect content changes.
    """
    cache = load_cache()
    path = Path(g_path)
    doc_id = _doc_id(g_path)
    cs = _checksum(path) if path.exists() else "missing"
    existing = cache.get(doc_id)
    if existing and existing.get("checksum") == cs:
        return existing
    meta = {
        "doc_id": doc_id,
        "g_path": g_path,
        "source_type": source_type,
        "title": title or path.stem,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "owner": owner,
        "tags": tags or [],
        "checksum": cs,
        "indexed_at": datetime.now().isoformat(),
    }
    cache[doc_id] = meta
    save_cache(cache)
    return meta


def remove(g_path: str) -> None:
    """Remove a document from the cache."""
    cache = load_cache()
    doc_id = _doc_id(g_path)
    if doc_id in cache:
        del cache[doc_id]
        save_cache(cache)


def list_all() -> list[dict[str, Any]]:
    """Return all cached document metadata, sorted by date desc."""
    cache = load_cache()
    docs = list(cache.values())
    docs.sort(key=lambda d: d.get("date", ""), reverse=True)
    return docs


def filter_by_source(source_type: str) -> list[dict[str, Any]]:
    """Filter documents by source_type (email / meeting / upload)."""
    return [d for d in list_all() if d.get("source_type") == source_type]


def search(query: str) -> list[dict[str, Any]]:
    """Simple title/tag search over cached metadata (no Chroma needed)."""
    q = query.lower()
    return [
        d
        for d in list_all()
        if q in (d.get("title") or "").lower()
        or any(q in t.lower() for t in (d.get("tags") or []))
    ]


def get_stats() -> dict[str, int]:
    """Return summary stats for the dashboard."""
    docs = list_all()
    by_source: dict[str, int] = {}
    for d in docs:
        st = d.get("source_type", "unknown")
        by_source[st] = by_source.get(st, 0) + 1
    return {"total": len(docs), **by_source}
