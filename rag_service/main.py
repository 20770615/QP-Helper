"""FastAPI backend for the Quam knowledge-base chat.

Run as a local background service (Option B from the integration prompt):
    python rag_service/main.py
and the PyQt app talks to it over http://127.0.0.1:8765.

Design notes
------------
* The service lives in ``rag_service/`` (sibling of the ``rag/`` package). We add
  the project root (the parent of ``rag_service/``) to ``sys.path`` at startup so
  ``import rag`` resolves regardless of cwd (dev script or frozen exe).
* LLM configuration (base_url / api_key / model) is injected externally — from
  environment variables at boot, or updated at runtime via ``POST /config``. No
  key is ever hardcoded here.
* ``/ask`` does NOT pre-retrieve: it hands the query to the harness, which owns
  retrieval (via its RAG tool or a direct ``rag.retrieve`` call). This keeps a
  single source of retrieval and avoids duplicate work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# ── make ``import rag`` and ``import harness`` work from anywhere ──────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../RAG
SERVICE_DIR = Path(__file__).resolve().parent           # .../RAG/rag_service
for d in (str(SERVICE_DIR), str(PROJECT_ROOT)):
    if d not in sys.path:
        sys.path.insert(0, d)

# ── bundled model cache (lite package) ────────────────────────────────────
# The lite distribution ships the HuggingFace cache (embedding + docling
# models) inside `_internal/embedding_model_cache` so a colleague can ingest
# from the shared drive even without external network. Point HF_HOME at it
# BEFORE anything imports/loads a model. In dev mode the folder does not
# exist, so the default per-user cache is used unchanged.
_BUNDLED_CACHE = None
if getattr(sys, "frozen", False):
    _cand = Path(sys.executable).resolve().parent / "_internal" / "embedding_model_cache"
    if _cand.is_dir():
        _BUNDLED_CACHE = str(_cand)
if _BUNDLED_CACHE and not os.environ.get("HF_HOME"):
    os.environ["HF_HOME"] = _BUNDLED_CACHE
    print(f"[rag_service] HF_HOME -> bundled model cache: {_BUNDLED_CACHE}", flush=True)

from rag.ingest import (
    ingest_directory, ingest_file, prune_missing,
    _stable_doc_id, _file_content_hash,
)  # noqa: E402
from rag.retrieve import retrieve                      # noqa: E402
from rag import config as rag_config                   # noqa: E402
from rag.store import get_collection                   # noqa: E402
from rag.exclusions import add_exclusion, is_excluded, load_exclusions  # noqa: E402

from fastapi import FastAPI, HTTPException             # noqa: E402
from pydantic import BaseModel                          # noqa: E402

import uvicorn                                          # noqa: E402

import logging
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag_service")

# Background auto-sync state (see _auto_sync_loop).
_AUTO_SYNC_STOP = threading.Event()
_AUTO_SYNC_RUNNING = threading.Lock()

EMAIL_CACHE = PROJECT_ROOT / "rag_service" / ".email_cache"

# In-memory LLM config; seeded from the app config file + env, mutable via POST /config.
CFG: dict = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "user_id": "shared",
    "scope": "shared",
}


# ─────────────────────────────────────────────────────────────────────────────
# KB readiness state (Option A: real progress for the frontend loading bar)
# ─────────────────────────────────────────────────────────────────────────────
# The embedding model (sentence-transformers, ~964 MB) is lazy-loaded on the
# first retrieval — that cold start is exactly what used to time out a query
# right after launch. We pre-load it in a background thread at startup and
# publish real progress here; the frontend polls GET /status and shows a bar.
_KB_STATE_LOCK = threading.Lock()
KB_STATE = {
    "status": "starting",      # starting | downloading | loading | ready | error
    "progress": 0,             # 0..100
    "message": "服务启动中…",
    "model_loaded": False,
}


def _set_kb(status=None, progress=None, message=None, model_loaded=None):
    with _KB_STATE_LOCK:
        if status is not None:
            KB_STATE["status"] = status
        if progress is not None:
            KB_STATE["progress"] = progress
        if message is not None:
            KB_STATE["message"] = message
        if model_loaded is not None:
            KB_STATE["model_loaded"] = model_loaded


def _kb_download_progress(*args):
    """Map huggingface_hub snapshot_download progress into KB_STATE.

    The callback signature changed across huggingface_hub versions
    ((repo_id, info) vs (info,)), so take the last arg and read its
    .total / .completed attributes defensively.
    """
    info = args[-1] if args else None
    if info is None:
        return
    total = getattr(info, "total", None)
    completed = getattr(info, "completed", None)
    if not total:
        return
    frac = min(1.0, (completed or 0) / total)
    _set_kb(status="downloading", progress=int(8 + 42 * frac),
            message=f"正在下载向量化模型… {int(frac * 100)}%")


def _warmup_embeddings():
    """Pre-load the embedding model + LanceDB so the first user query is fast.

    Runs in a daemon thread at service startup. Reports real progress through
    KB_STATE, which the frontend polls via GET /status. On failure we do NOT
    block the UI: the first /ask will lazy-load the model on demand.
    """
    try:
        model_name = getattr(rag_config, "EMBEDDING_MODEL",
                              "paraphrase-multilingual-MiniLM-L12-v2")
        # Phase 1 — make sure model files are local (download on first run).
        _set_kb(status="downloading", progress=5, message="正在准备向量化模型…")
        try:
            import inspect
            from huggingface_hub import snapshot_download
            # huggingface_hub>=0.26 dropped progress_callback; older builds still
            # accept it. Prefer it when present so /status can show download %.
            kwargs: dict = {"repo_id": model_name, "local_files_only": False}
            if "progress_callback" in inspect.signature(snapshot_download).parameters:
                kwargs["progress_callback"] = _kb_download_progress
            snapshot_download(**kwargs)
        except Exception as e:
            logger.warning("[warmup] model pre-download skipped: %s", e)
        # Phase 2 — load the model + LanceDB via the REAL retrieval path, so the
        # SAME _EF singleton the app uses is warmed (no double model load).
        _set_kb(status="loading", progress=60, message="正在加载向量化模型…")
        retrieve("warmup placeholder text 预热占位文本", n=1)
        _set_kb(status="ready", progress=100, message="知识库已就绪",
                model_loaded=True)
        logger.info("[warmup] embedding model + KB ready")
    except Exception as e:
        logger.exception("[warmup] KB warmup failed")
        _set_kb(status="error", progress=100,
                message=f"模型预热失败：{e}（仍可在提问时加载）",
                model_loaded=False)


def _config_path():
    """Locate the app's config file — the source of truth for LLM settings.

    The app passes the exact path via ``RAG_CONFIG_PATH`` when it starts the
    service, so this is normally authoritative. We still keep fallback
    candidates (mirroring the app's ``CONFIG_FILE`` resolution) so the service
    can recover the key even if the env var is missing.
    """
    env_path = os.environ.get("RAG_CONFIG_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    candidates = [
        SERVICE_DIR.parent / "qp_email_helper.config.json",                 # RAG/  (dev)
        SERVICE_DIR.parent.parent / "qp_email_helper.config.json",           # project root (dev)
        Path(sys.executable).parent / "qp_email_helper.config.json",         # frozen exe dir
        Path(sys.executable).parent / "_internal" / "qp_email_helper.config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _resolve_holdings_path(cfg: dict) -> Path | None:
    """Resolve the UCITS holdings file from config, with shared-root fallback.

    The explicit ``holdings_csv_path`` setting takes precedence (name kept for
    backward compatibility — the file may be CSV / Excel / JSON). If it is
    missing or the file does not exist, fall back to common default names under
    ``shared_root`` (``ucits_fund_holdings.csv`` / ``.xlsx`` / …).
    """
    h = (cfg.get("holdings_csv_path") or "").strip()
    if h:
        p = Path(h)
        if p.exists():
            return p
    root = (cfg.get("shared_root") or "").strip()
    if root:
        try:
            from rag.holdings_io import find_default_holdings
            found = find_default_holdings(root)
            if found:
                return found
        except Exception:
            p = Path(root) / "ucits_fund_holdings.csv"
            if p.exists():
                return p
    return None


def _normalize_llm_model(model) -> str:
    """Canonicalize DeepSeek (etc.) model ids — APIs reject wrong casing."""
    raw = (model or "").strip() if isinstance(model, str) else ""
    if not raw:
        return ""
    key = raw.lower().replace("_", "-").replace(" ", "")
    aliases = {
        "deepseek-flash": "deepseek-flash",
        "deepseek-v4-flash": "deepseek-flash",
        "deepseekv4flash": "deepseek-flash",
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-v4pro": "deepseek-v4-pro",
        "deepseekv4pro": "deepseek-v4-pro",
    }
    if key in aliases:
        return aliases[key]
    if key.startswith("deepseek"):
        return key
    return raw


def _resolve_cfg() -> dict:
    """Build the effective LLM config.

    Priority (lowest → highest):
      1. values persisted in the app's config.json  (RAG_CONFIG_PATH / fallback)
      2. RAG_LLM_* environment variables (injected at service start)
      3. live overrides pushed via POST /config (in-memory CFG)
    """
    cfg = {"base_url": "", "api_key": "", "model": "", "auto_retrieve": False,
           "kb_triggers": None}
    p = _config_path()
    if p:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            cfg["base_url"] = (d.get("llm_base_url") or "").strip()
            cfg["api_key"] = (d.get("llm_api_key") or "").strip()
            cfg["model"] = _normalize_llm_model(d.get("llm_model"))
            cfg["auto_retrieve"] = bool(d.get("auto_retrieve", False))
            # User-defined KB-retrieval trigger words. None / empty => the
            # harness falls back to its built-in list. Stored as a JSON array
            # of strings in the config file.
            _kt = d.get("kb_triggers")
            if isinstance(_kt, list) and all(isinstance(x, str) for x in _kt):
                cfg["kb_triggers"] = _kt
        except Exception:
            pass
    cfg["base_url"] = (os.environ.get("RAG_LLM_BASE_URL") or "").strip() or cfg["base_url"]
    cfg["api_key"] = (os.environ.get("RAG_LLM_API_KEY") or "").strip() or cfg["api_key"]
    cfg["model"] = _normalize_llm_model(
        (os.environ.get("RAG_LLM_MODEL") or "").strip() or cfg["model"]
    )
    # Live /config override wins if present (so a runtime key change isn't lost).
    if CFG.get("api_key"):
        cfg["api_key"] = CFG["api_key"]
    if CFG.get("base_url"):
        cfg["base_url"] = CFG["base_url"]
    if CFG.get("model"):
        cfg["model"] = _normalize_llm_model(CFG["model"])
    cfg["model"] = _normalize_llm_model(cfg.get("model"))
    if "auto_retrieve" in CFG:
        cfg["auto_retrieve"] = bool(CFG["auto_retrieve"])
    if "kb_triggers" in CFG:
        cfg["kb_triggers"] = CFG["kb_triggers"]
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────────
class IngestEmailsReq(BaseModel):
    json_path: str
    user_id: str = "shared"
    scope: str = "shared"


class IngestDirReq(BaseModel):
    dir: str
    user_id: str = "shared"
    scope: str = "shared"


class IngestFilesReq(BaseModel):
    paths: list[str]
    user_id: str = "shared"
    scope: str = "shared"


class RemoveReq(BaseModel):
    """Remove document vectors FROM the DB (keep the local file).

    ``paths`` are the source file paths (the same absolute paths used at ingest
    time). Each file's content_hash is recorded in the exclusion list so the
    passive auto-sync poller will NOT re-embed it. Manual re-ingestion bypasses
    the exclusion list, so the file can always be forced back in.
    """
    paths: list[str]
    user_id: str = "shared"
    scope: str = "shared"


class RemoveFolderReq(BaseModel):
    """Remove an entire folder (and its sub-folders) from the vector DB.

    Local files are KEPT. All existing vector rows for files under the folder
    are deleted, and the folder prefix is recorded in the exclusion list so
    auto-sync and future "Ingest KB" runs skip it.
    """
    folder: str
    user_id: str = "shared"
    scope: str = "shared"


class QueryReq(BaseModel):
    query: str
    scope: str | None = None
    user_id: str | None = None
    n: int = 5


class AskReq(BaseModel):
    query: str
    scope: str | None = None
    user_id: str | None = None
    n: int = 6
    history: list[dict] = []
    force_retrieve: bool = False
    filter_paths: list[str] = []


class GenerateReportReq(BaseModel):
    """Request to render the UCITS daily report DOCX.

    ``output_dir`` is the user-chosen "产出空间" — the report is saved there as
    ``YYYYMMDD_Global_Equity_Opportunities_Report_UCITS.docx``.
    """
    json_path: str
    holdings_csv: str | None = None
    output_dir: str
    language: str = "zh"
    date: str | None = None


class ConfigReq(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    user_id: str | None = None
    scope: str | None = None
    auto_retrieve: bool | None = None
    kb_triggers: list[str] | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Email -> Markdown ingestion
# ─────────────────────────────────────────────────────────────────────────────
def _safe_name(s: str, limit: int = 60) -> str:
    s = (s or "email").strip()
    s = re.sub(r"[^\w一-鿿 .-]", "_", s)
    return s[:limit].strip() or "email"


def _email_doc_id(meta: dict) -> str:
    key = f"{meta.get('subject','')}|{meta.get('sender','')}|{meta.get('time','')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


# Outlook timestamps look like "Wednesday, August 5, 2026 11:11:14 PM (UTC+08:00)
# ...". Slice-based parsing (time[:10]) would yield "Wednesday," — useless as a
# filter key. Parse the month name explicitly into a stable YYYY-MM-DD.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_DATE_PARSE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})")


def _parse_email_date(time_str: str) -> str | None:
    """Extract a ``YYYY-MM-DD`` calendar date from an Outlook email timestamp.

    Returns None when the timestamp is missing / unparseable so callers can fall
    back to storing email_date = None (date-bounded queries then simply won't
    match that chunk, which is the correct degenerate behaviour).
    """
    if not time_str or time_str.lower() == "none":
        return None
    m = _DATE_PARSE_RE.search(time_str)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    try:
        return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"
    except Exception:
        return None


def _ingest_emails(json_path: str, user_id: str, scope: str) -> int:
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="expected a JSON list of emails")

    EMAIL_CACHE.mkdir(parents=True, exist_ok=True)
    total = 0
    for i, mail in enumerate(data):
        subject = str(mail.get("subject", ""))
        sender = str(mail.get("sender", ""))
        time = str(mail.get("time", ""))
        body = str(mail.get("body", "") or mail.get("content", ""))
        if not body.strip():
            continue
        # Parse the email's own calendar date for date-bounded retrieval.
        email_date = _parse_email_date(time)
        doc_hash = _email_doc_id(mail)
        fname = (
            f"{email_date}_{_safe_name(subject)}_{doc_hash}.md"
            if email_date
            else f"{_safe_name(subject)}_{doc_hash}.md"
        )
        md = (
            f"# {subject}\n"
            f"- From: {sender}\n"
            f"- Time: {time}\n"
            f"- Source: Outlook email (fetched by QP Email Helper)\n\n"
            f"{body}\n"
        )
        out = EMAIL_CACHE / fname
        out.write_text(md, encoding="utf-8")
        # Pass the email's own calendar date (not the ingest time) so date
        # queries ("昨天的邮件") can SQL-filter to that day's chunks.
        total += ingest_file(out, user_id=user_id, scope=scope, doc_date=email_date)
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Remove-from-DB (keep local file, exclude from auto-sync)
# ─────────────────────────────────────────────────────────────────────────────
def _rel_of(rp: Path) -> str:
    """Best-effort posix rel_path of ``rp`` under the KB root.

    Echoed back to the frontend so the UI can drop the matching bubble without
    fighting Windows ``\\\\?\\`` prefixes or drive-letter/separator normalisation.
    Returns "" if it can't be derived.
    """
    try:
        root = _kb_root()
        if root:
            rel = rp.resolve().relative_to(Path(root).resolve())
            return rel.as_posix()
    except Exception:
        pass
    return ""


def _remove_from_db(paths: list[str]) -> dict:
    """Delete vector rows for each source path and exclude its content_hash.

    The local file is KEPT. The file's content_hash is recorded in the exclusion
    list so the passive auto-sync poller will not silently re-embed it. Manual
    re-ingestion (Ingest KB / import files) bypasses the list and can force the
    file back in. Returns {"removed": [...], "excluded": [...], "errors": [...]}.
    """
    collection = get_collection()
    removed, removed_rel, excluded, errors = [], [], [], []
    for raw in paths:
        p = Path(raw)
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        doc_id = _stable_doc_id(rp)
        rel = _rel_of(rp)
        content_hash = None
        chunk_count = 0
        # Read chunk-0 metadata for the stored hash + chunk count (cheap, single
        # point lookup — no full table scan).
        try:
            metas = collection.get_metadatas([f"{doc_id}_0"])
            meta = metas[0] if metas else None
            if meta:
                content_hash = meta.get("content_hash")
                chunk_count = int(meta.get("chunk_count") or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("[remove] metadata read failed for %s: %s", rp, e)
        # Prefer the live on-disk hash (what auto-sync will compute next tick);
        # fall back to the stored hash if the file is already gone.
        if rp.exists():
            try:
                content_hash = _file_content_hash(rp)
            except Exception:
                pass
        # Delete the vector rows for this doc_id.
        try:
            if chunk_count and chunk_count > 0:
                ids = [f"{doc_id}_{i}" for i in range(chunk_count)]
                collection.delete(ids=ids)
            elif hasattr(collection, "delete_by_doc_id"):
                # Legacy rows (no chunk_count recorded) — full-scan fallback.
                collection.delete_by_doc_id(doc_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{rp.name}: delete error {e}")
        # Record the exclusion so auto-sync won't re-embed it. Store the real
        # posix rel_path (not the resolved abs path) so the UI can hide it.
        if content_hash:
            try:
                if add_exclusion(content_hash, rel):
                    excluded.append(str(rp))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{rp.name}: exclude error {e}")
        removed.append(str(rp))
        if rel:
            removed_rel.append(rel)
    return {"removed": removed, "removed_rel": removed_rel,
            "excluded": excluded, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Quam RAG Service", version="1.0.0")


# Global exception handler — catch EVERY unhandled exception so the frontend
# gets a useful JSON response instead of a raw HTTP 500 with no body.
@app.exception_handler(Exception)
async def _global_exception_handler(request, exc):
    import traceback
    tb = traceback.format_exc(limit=10)
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url, exc, extra={"traceback": tb})
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={
            "answer": f"[服务内部错误] {type(exc).__name__}: {exc}",
            "citations": [],
            "used_llm": False,
            "note": "unhandled_error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": tb,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def kb_status():
    """KB readiness for the frontend loading bar (Option A).

    Returns the live state of the background warmup thread: whether the
    embedding model is still downloading/loading, or ready to answer.
    """
    with _KB_STATE_LOCK:
        return {
            "status": KB_STATE["status"],
            "progress": KB_STATE["progress"],
            "message": KB_STATE["message"],
            "model_loaded": KB_STATE["model_loaded"],
        }


@app.post("/ingest/emails")
def ingest_emails(req: IngestEmailsReq):
    try:
        n = _ingest_emails(req.json_path, req.user_id, req.scope)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"ingest failed: {e}")
    return {"chunks": n, "source": req.json_path}


@app.post("/ingest/dir")
def ingest_dir(req: IngestDirReq):
    p = Path(req.dir)
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {req.dir}")
    # Serialize with the auto-sync poller so two crawls never touch the
    # LanceDB table at once (the poller also holds _AUTO_SYNC_RUNNING).
    with _AUTO_SYNC_RUNNING:
        # Recursive, resilient ingest — returns a summary dict
        # {"files", "chunks", "skipped", "errors", "pruned"}. On an explicit
        # "Ingest KB" click we also prune deletions: a single walk of the share
        # is cheap enough for a deliberate user action (see ingest.run_prune).
        # The exclusion list is respected so files/folders the user previously
        # removed from the DB stay gone. Individual file uploads still bypass it
        # and can force a removed file back in.
        res = ingest_directory(p, user_id=req.user_id, scope=req.scope,
                               run_prune=True, respect_exclusions=True)
    return {"dir": req.dir, **res}


@app.post("/remove")
def remove_from_db(req: RemoveReq):
    """Delete a document's vectors from the LanceDB store (keep the local file).

    The file's ``content_hash`` is recorded in the exclusion list so the passive
    auto-sync poller will not silently re-embed it. Returns per-file results.
    Serialized with the auto-sync poller (like /ingest/dir) so the LanceDB
    table is never written by two crawlers at once.
    """
    with _AUTO_SYNC_RUNNING:
        res = _remove_from_db(req.paths)
    return res


@app.post("/remove/folder")
def remove_folder(req: RemoveFolderReq):
    """Delete all vectors under a folder and exclude the folder from future ingest.

    Local files are kept. Existing vector rows for every supported file under
    the folder are removed, and the folder prefix is written to the exclusion
    list so auto-sync and "Ingest KB" skip it going forward.
    """
    folder_rel = req.folder.strip().strip("/")
    if not folder_rel:
        raise HTTPException(status_code=400, detail="folder is required")
    root = _kb_root()
    if not root:
        raise HTTPException(status_code=400, detail="KB root unavailable")
    folder_path = root / folder_rel
    if not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(status_code=400, detail=f"not a folder: {req.folder}")

    # Collect every supported file recursively (but not inside skipped dirs).
    paths: list[str] = []
    for p in folder_path.rglob("*"):
        if p.is_file() and p.suffix.lower() in rag_config.SUPPORTED_SUFFIXES:
            paths.append(str(p))

    with _AUTO_SYNC_RUNNING:
        if paths:
            _remove_from_db(paths)
        from rag.exclusions import add_folder_exclusion
        excluded = add_folder_exclusion(folder_rel)

    return {
        "folder": folder_rel,
        "files_removed": len(paths),
        "exclusion_added": excluded,
    }


@app.get("/exclusions")
def list_exclusions():
    """Debug/visibility: return the current user-exclusion list."""
    return {"exclusions": load_exclusions(), "count": len(load_exclusions())}


@app.post("/ingest/files")
def ingest_files(req: IngestFilesReq):
    """Ingest a user-selected list of individual files (no folder needed).

    Mirrors the UI "导入资料" button: accepts arbitrary supported files
    (.pdf/.docx/.pptx/.html/.txt/.md/.csv) and upserts each via ingest_file.
    Unsupported / missing files are reported under ``skipped`` rather than 400,
    so a partial selection still succeeds.
    """
    ingested, skipped = [], []
    for raw in req.paths:
        p = Path(raw)
        if not p.exists() or not p.is_file():
            skipped.append(f"{p.name} (not found)")
            continue
        if p.suffix.lower() not in rag_config.SUPPORTED_SUFFIXES:
            skipped.append(f"{p.name} (unsupported type)")
            continue
        try:
            n = ingest_file(p, user_id=req.user_id, scope=req.scope,
                            respect_exclusions=False)
            ingested.append({"name": p.name, "chunks": n})
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{p.name} ({e})")
    total = sum(item["chunks"] for item in ingested)
    return {"chunks": total, "ingested": ingested, "skipped": skipped}


@app.post("/query")
def query(req: QueryReq):
    # Raw retrieval preview (no LLM). Returns the underlying chunks.
    try:
        return retrieve(req.query, scope=req.scope, user_id=req.user_id, n=req.n)
    except Exception as e:
        import traceback
        return {
            "note": "error",
            "error": str(e),
            "traceback": traceback.format_exc(limit=10),
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }


@app.post("/ask")
async def ask(req: AskReq):
    # Inject current holdings (from Settings) into every query so the agent
    # always has portfolio context without relying on KB retrieval or the
    # user having explicitly told it.
    #
    # IMPORTANT: We must use THIS module's _config_path() (which respects
    # RAG_CONFIG_PATH set by the frontend) — NOT config_manager.get_holdings_csv_path()
    # which resolves APP_ROOT via sys.executable and points to the WRONG config
    # file when running as a subprocess (the .venv pythonw.exe, not the exe).
    query = req.query
    # Holdings injection applies ONLY when (a) the question is NOT scoped to a
    # folder/file via @source (filter_paths non-empty) AND (b) the question is
    # actually about the portfolio. Pre-pending the Settings holdings text to
    # EVERY query made the agent force UCITS holdings into unrelated answers
    # (e.g. "summarize the news in this folder" → a holdings table), which the
    # user explicitly rejected. Now the holdings are injected only for
    # portfolio-related questions; everything else stays clean.
    _PORTFOLIO_KW = (
        "portfolio", "holdings", "position", "positions", "rebalance",
        "ucits", "book", "allocation", "exposure", "weight",
        "组合", "持仓", "仓位", "调仓", "换仓", "配置", "股票占比", "权重",
    )
    _q = req.query.lower()
    if not req.filter_paths and any(k in _q for k in _PORTFOLIO_KW):
        try:
            cfg_path = _config_path()
            if cfg_path:
                d = json.loads(cfg_path.read_text(encoding="utf-8"))
                h_file = _resolve_holdings_path(d)
                if h_file and h_file.exists():
                    try:
                        from rag.holdings_io import load_holdings_text
                        h_text = load_holdings_text(h_file)
                    except Exception:
                        h_text = h_file.read_text(encoding="utf-8-sig").strip()
                    if h_text and len(h_text) > 5:
                        query = (
                            f"[Current UCITS Fund Portfolio Holdings (from Settings)]\n"
                            f"{h_text}\n\n"
                            f"[User Question]\n{req.query}"
                        )
        except Exception:
            pass  # holdings injection is best-effort; never block the query

    # Always resolve the latest LLM config (file + env + live /config override)
    # so a saved API key is picked up even if POST /config was never reached.
    try:
        from harness import get_harness  # local import keeps startup light
        harness = get_harness(_resolve_cfg())
        # scope / user_id are audit/attribution metadata only — the harness
        # ignores them (retrieval is unfiltered in the shared team KB), so we
        # do NOT forward them to harness.answer() (its signature is fixed).
        result = await harness.answer(
            query,
            contexts=[],          # retrieval is owned by the harness
            history=req.history,
            n=req.n,
            force_retrieve=req.force_retrieve,
            filter_paths=req.filter_paths,
        )
    except Exception as e:  # surface the REAL cause, never a bare 500
        import traceback
        tb = traceback.format_exc(limit=8)
        # Return 200 with the error embedded so the UI can show the actual
        # reason (bad key / wrong model / network / import) instead of "500".
        return {
            "answer": f"[知识库调用出错] {e}",
            "citations": [],
            "used_llm": False,
            "note": "error",
            "error": str(e),
            "traceback": tb,
        }
    return {
        "answer": result.answer,
        "citations": [
            {
                "source": c.source,
                "snippet": c.snippet,
                "score": c.score,
                "page": c.page,
                "chunk_index": c.chunk_index,
            }
            for c in result.citations
        ],
        "used_llm": result.used_llm,
        "note": result.note,
    }


@app.post("/config")
def config(req: ConfigReq):
    if req.base_url is not None:
        CFG["base_url"] = req.base_url
    if req.api_key is not None:
        CFG["api_key"] = req.api_key
    if req.model is not None:
        CFG["model"] = _normalize_llm_model(req.model)
    if req.user_id is not None:
        CFG["user_id"] = req.user_id
    if req.scope is not None:
        CFG["scope"] = req.scope
    if req.auto_retrieve is not None:
        CFG["auto_retrieve"] = bool(req.auto_retrieve)
    if req.kb_triggers is not None:
        CFG["kb_triggers"] = list(req.kb_triggers)
    return {"ok": True, "cfg": {k: ("***" if k == "api_key" else v) for k, v in CFG.items()}}


@app.post("/generate-report")
async def generate_report(req: GenerateReportReq):
    """Generate the UCITS daily-report DOCX from fetched emails + holdings.

    Pipeline: resolve LLM cfg → ask the harness to produce the report **body**
    as Markdown → render it to a .docx (locked cover + styling) in
    ``output_dir``. Returns the saved path so the app can open it.
    """
    from datetime import datetime

    if not Path(req.json_path).exists():
        raise HTTPException(status_code=400, detail=f"emails json not found: {req.json_path}")
    out = Path(req.output_dir)
    if not out.exists() or not out.is_dir():
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid output_dir: {e}")
    date = req.date or datetime.now().strftime("%Y-%m-%d")

    try:
        import report_gen
        import report_docx

        cfg = _resolve_cfg()
        if not cfg.get("api_key"):
            return {
                "ok": False,
                "error": "no_llm_key",
                "message": "未在设置中配置 LLM API Key，无法生成报告。",
            }
        md = await report_gen.generate_daily_report(
            cfg, req.json_path, req.holdings_csv, req.language, date
        )
        path = report_docx.build_report(md, str(out), date, req.language)
    except Exception as e:  # noqa: BLE001 — surface the real cause, never bare 500
        import traceback
        tb = traceback.format_exc(limit=10)
        return {"ok": False, "error": str(e), "traceback": tb}

    return {"ok": True, "path": str(path), "filename": path.name, "date": date}


def _seed_cfg_from_env():
    # Seed in-memory CFG from the persistent config file + env so a freshly
    # started service already has the key (the app also injects RAG_LLM_* env
    # and the config-file path). POST /config can still override at runtime.
    seeded = _resolve_cfg()
    CFG["base_url"] = seeded["base_url"]
    CFG["api_key"] = seeded["api_key"]
    CFG["model"] = seeded["model"]


def _kb_root() -> Path | None:
    """Resolve the knowledge-base root the auto-sync poller should crawl.

    Mirrors the app: the shared drive root from qp_email_helper.config.json
    (key ``shared_root``), falling back to the documented default. Returns None
    if unresolved / not mounted so the poller stays silent instead of crashing.
    """
    root = None
    p = _config_path()
    if p:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            root = (d.get("shared_root") or "").strip()
        except Exception:
            pass
    if not root:
        root = "G:/QAML-qam/Investment Team/"
    path = Path(root)
    return path if path.exists() else None


def _auto_sync_loop(interval_minutes: int) -> None:
    """Background loop: periodically re-ingest the KB so newly dropped files are
    picked up without the user clicking 'Ingest KB'.

    Each tick embeds NEW / CHANGED files only (``run_prune=False``) — that is
    fast and never does a full-share filesystem walk, so new files land in the
    KB within ~one interval instead of being blocked behind a 395k-row prune.

    Deletion cleanup (prune_missing) is decoupled and runs on its own SLOW
    schedule (``config.AUTO_PRUNE_INTERVAL_SECONDS`` == 24h). When it runs it
    does a SINGLE os.walk() of the share (cheap on SMB) rather than 395k
    per-vector network checks. A safety rail inside prune_missing() aborts if
    >50% of sources vanish (share unmounted), so a dropped network drive never
    nukes the KB. Overlap with a slow previous tick is prevented by
    ``_AUTO_SYNC_RUNNING``.
    """
    logger.info("[auto-sync] loop started, interval=%s min", interval_minutes)
    last_prune = 0.0
    prune_every = rag_config.AUTO_PRUNE_INTERVAL_SECONDS
    while not _AUTO_SYNC_STOP.is_set():
        # Wait for the interval; wake immediately if asked to stop.
        if _AUTO_SYNC_STOP.wait(interval_minutes * 60):
            break
        if _AUTO_SYNC_RUNNING.locked():
            continue  # previous tick still running; skip this one
        kb = _kb_root()
        if not kb:
            logger.warning("[auto-sync] KB root unavailable; skipping tick")
            continue
        with _AUTO_SYNC_RUNNING:
            try:
                # 1) Embed new/changed files only (fast, no share walk).
                logger.info("[auto-sync] syncing KB: %s", kb)
                res = ingest_directory(kb, user_id="shared", scope="shared",
                                       run_prune=False)
                pruned_rows = None
                # 2) Prune deletions on a slow schedule (single cheap walk).
                now = time.time()
                if prune_every > 0 and now - last_prune >= prune_every:
                    try:
                        pr = prune_missing(dry_run=False, root=kb)
                        pruned_rows = pr.get("deleted_rows")
                        last_prune = now
                        logger.info(
                            "[auto-sync] pruned sources=%s rows=%s%s",
                            pr.get("deleted_sources"), pruned_rows,
                            " (aborted)" if pr.get("aborted") else "",
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("[auto-sync] prune failed")
                logger.info(
                    "[auto-sync] done files=%s chunks=%s pruned=%s errors=%s",
                    res.get("files"), res.get("chunks"), pruned_rows,
                    len(res.get("errors") or []),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[auto-sync] tick failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quam RAG backend service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-auto-sync", action="store_true",
                        help="disable the background KB auto-sync poller")
    args = parser.parse_args()

    _seed_cfg_from_env()
    interval = rag_config.AUTO_SYNC_INTERVAL_MINUTES
    if args.no_auto_sync:
        interval = 0
    if interval and interval > 0:
        threading.Thread(
            target=_auto_sync_loop, args=(interval,), daemon=True
        ).start()
        logger.info("[auto-sync] enabled every %s min (KB=%s)", interval, _kb_root())
    else:
        logger.info("[auto-sync] disabled")

    # Warm up the embedding model + KB in the background so the first user
    # query does not hit a cold-start timeout. The frontend polls /status.
    threading.Thread(target=_warmup_embeddings, daemon=True).start()
    logger.info("[warmup] background KB warmup started")

    def _run_server():
        # Bind with retry: when we kill a previous instance of THIS package the
        # port can sit in TIME_WAIT for a moment, and binding immediately would
        # otherwise raise "Address already in use" and crash the service on
        # startup. uvicorn sets SO_REUSEADDR, so a short backoff is enough. We
        # never loop forever — after N attempts we exit so the parent app can
        # show a clear "service failed" state instead of a silent hang.
        max_attempts = 10
        for attempt in range(1, max_attempts + 1):
            try:
                uvicorn.run(app, host=args.host, port=args.port, log_level="info")
                return
            except OSError as exc:  # address-in-use / bind failure
                logger.warning(
                    "[startup] bind attempt %d/%d failed: %s",
                    attempt, max_attempts, exc,
                )
                time.sleep(1.5)
        logger.error(
            "[startup] could not bind %s:%d after %d attempts; exiting",
            args.host, args.port, max_attempts,
        )
        sys.exit(1)

    _run_server()
