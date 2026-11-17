"""User-managed exclusion list for the KB vector store.

When a user *removes a document from the database* (deleting its LanceDB
vectors but keeping the local file), we record the file's ``content_hash`` here
so the passive auto-sync poller never re-embeds it. Re-uploading / explicitly
re-ingesting the file bypasses this list, so the user can always force it back
in.

Persistence
-----------
Stored as JSON next to the running app (``<app>/_internal/exclusions.json``),
so it is per-machine and fully separate from the LanceDB store itself. The file
lives beside ``exclusions.py``'s parent package (``rag/`` -> ``_internal/``).

Concurrency
-----------
``add_exclusion`` is guarded by a lock and also keeps an in-memory cache that is
reloaded only when the on-disk file's mtime changes. That lets the auto-sync
poller call ``is_excluded`` once per file per tick WITHOUT re-reading the file
hundreds of thousands of times.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
_cache: set[str] | None = None
_folder_cache: set[str] | None = None
_cache_mtime: float = 0.0


def _path() -> Path:
    # exclusions.py lives in <app>/_internal/rag/ -> parent.parent == <app>/_internal/
    return Path(__file__).resolve().parent.parent / "exclusions.json"


def _reload_if_needed() -> None:
    """Refresh the in-memory cache when the on-disk file has changed."""
    global _cache, _folder_cache, _cache_mtime
    p = _path()
    if not p.exists():
        if _cache is not None:
            _cache = set()
            _folder_cache = set()
            _cache_mtime = 0.0
        return
    try:
        mtime = p.stat().st_mtime
    except Exception:
        return
    if _cache is not None and mtime == _cache_mtime:
        return
    try:
        recs = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        recs = []
    _cache = {
        r["content_hash"]
        for r in recs
        if isinstance(r, dict) and r.get("content_hash")
    }
    _folder_cache = {
        r["folder"]
        for r in recs
        if isinstance(r, dict) and r.get("folder")
    }
    _cache_mtime = mtime


def load_exclusions() -> list[dict]:
    """Return the full list of exclusion records (for debugging / UI)."""
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def is_excluded(content_hash: str) -> bool:
    """True if ``content_hash`` is in the exclusion set.

    Cheap: reads the JSON file only when its mtime changed.
    """
    if not content_hash:
        return False
    with _lock:
        _reload_if_needed()
        return content_hash in (_cache or set())


def add_exclusion(content_hash: str, rel_path: str | None = None) -> bool:
    """Add ``content_hash`` to the exclusion set.

    Returns True if a NEW entry was written, False if it was already present.
    Persists ``{content_hash, rel_path, removed_at}`` and updates the cache
    DIRECTLY (not via mtime reload) so ``is_excluded`` is immediately consistent
    within this process — filesystem mtime resolution can otherwise make a
    same-tick reload look like "no change" and leave the cache stale.
    """
    if not content_hash:
        return False
    with _lock:
        global _cache, _folder_cache, _cache_mtime
        recs = load_exclusions()
        if any(r.get("content_hash") == content_hash for r in recs if isinstance(r, dict)):
            return False
        recs.append({
            "content_hash": content_hash,
            "rel_path": rel_path or "",
            "removed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _save(recs)
        # Update the in-memory cache deterministically from the saved records.
        _cache = {
            r["content_hash"]
            for r in recs
            if isinstance(r, dict) and r.get("content_hash")
        }
        _folder_cache = {
            r["folder"]
            for r in recs
            if isinstance(r, dict) and r.get("folder")
        }
        try:
            _cache_mtime = _path().stat().st_mtime
        except Exception:
            _cache_mtime = 0.0
        return True


def is_folder_excluded(rel_path: str) -> bool:
    """True if ``rel_path`` lives inside an excluded folder prefix.

    Folder exclusions are stored as posix relative paths (e.g.
    ``knowledge_base/Outlook email reports``). A file matches when its own
    relative path equals that folder or starts with ``<folder>/``.
    """
    if not rel_path:
        return False
    rel = rel_path.replace("\\", "/").strip("/")
    with _lock:
        _reload_if_needed()
        for folder in (_folder_cache or set()):
            f = folder.replace("\\", "/").strip("/")
            if not f:
                continue
            if rel == f or rel.startswith(f + "/"):
                return True
    return False


def add_folder_exclusion(folder_rel: str) -> bool:
    """Add a folder prefix to the exclusion list.

    Returns True if a NEW entry was written, False if already present.
    Files inside this folder will be skipped by auto-sync and manual
    "Ingest KB", and hidden from the DB browser.
    """
    folder = (folder_rel or "").replace("\\", "/").strip("/")
    if not folder:
        return False
    with _lock:
        global _cache, _folder_cache, _cache_mtime
        recs = load_exclusions()
        if any(
            (r.get("folder") or "").replace("\\", "/").strip("/") == folder
            for r in recs if isinstance(r, dict)
        ):
            return False
        recs.append({
            "folder": folder,
            "excluded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _save(recs)
        _cache = {
            r["content_hash"]
            for r in recs
            if isinstance(r, dict) and r.get("content_hash")
        }
        _folder_cache = {
            r["folder"]
            for r in recs
            if isinstance(r, dict) and r.get("folder")
        }
        try:
            _cache_mtime = _path().stat().st_mtime
        except Exception:
            _cache_mtime = 0.0
        return True


def load_folder_exclusions() -> list[str]:
    """Return the list of excluded folder prefixes (posix, sorted)."""
    with _lock:
        _reload_if_needed()
        return sorted(_folder_cache or set())


def _save(records: list[dict]) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
