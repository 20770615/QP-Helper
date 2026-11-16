"""Ingestion: load documents, chunk them, and upsert into the Chroma collection.

Loaders
------
- .txt / .md / .csv  -> read natively (utf-8)
- .xlsx -> parse with pandas/openpyxl: each sheet becomes a Markdown table
  plus a header row so tabular news/feeds stay readable in chunks.
- .pdf / .docx / .pptx / .html -> parse with Docling (IBM, Apache-2.0), which
  preserves tables, layout, and heading structure, then export to Markdown.
  (Docling is local, needs no API key, downloads a small model on first parse.)

Every chunk carries the metadata schema required from day one:
    user_id, scope, source, doc_id, chunk_index, created_at
so that Phase 2 (login) can filter by user/scope without rewriting ingestion.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from rag import config
from rag.store import get_collection
from rag.exclusions import is_excluded, is_folder_excluded

# Dirs that must never be crawled during KB ingestion (system / build / deps).
# Mirrors the app's KB_SKIP_DIRS so the vector store stays clean.
INGEST_SKIP_DIRS = {
    ".venv", "venv", "build", "dist", "__pycache__",
    "node_modules", ".git", ".idea", ".vscode", "_internal",
    "workbuddy", ".workbuddy",
}

# Filename prefixes that mark Office/OS temp locks or hidden metadata — skip them.
# "_$" covers OneDrive's own lock artifacts (e.g. "_$M Trading Manual.docx").
_INGEST_TEMP_PREFIXES = ("~$", "_$", ".~", ".tmp", ".")

_DOC_CONVERTER = None


# ---------------------------------------------------------------------------
# Canonical source paths (OneDrive local root <-> UNC share root).
#
# The same SharePoint library is reachable as:
#   UNC  \\WWFS01\Group\QAML-qam\Investment Team   (also mapped as G:)
#   OneDrive  C:\Users\<user>\...\Group - Documents\QAML-qam\Investment Team
# The store's `source` metadata and every doc_id MUST use ONE canonical form.
# Without it, re-ingesting the same file from a different root would produce a
# different doc_id and silently DUPLICATE every chunk. The mapping is read from
# qp_email_helper.config.json["source_alias"] (a list of [local, canonical]
# pairs), injected by the app via RAG_CONFIG_PATH. No alias configured => no-op.
# ---------------------------------------------------------------------------
def _alias_map() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    p = os.environ.get("RAG_CONFIG_PATH") or ""
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8")) if p else {}
    except Exception:
        d = {}
    for pair in d.get("source_alias") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        loc = (pair[0] or "").replace("\\", "/").rstrip("/").lower()
        can = (pair[1] or "").replace("\\", "/").rstrip("/")
        if loc and can:
            out.append((loc, can))
    return out


def canonical_source(path) -> str:
    """Map a machine-local share path to the store's canonical (UNC) form.

    The canonical form is the Windows-native UNC string (backslash-separated,
    e.g. ``\\\\WWFS01\\Group\\QAML-qam\\Investment Team\\...``) — exactly what
    ``str(Path(...).resolve())`` yields for a UNC or mapped-drive path. That
    keeps doc_ids identical whether a file is ingested via the OneDrive local
    folder or via ``\\\\WWFS01\\...`` / ``G:\\...``. Identity when no alias
    matches (e.g. a colleague's machine that only ever uses UNC).
    """
    s = str(path)
    s_norm = s.replace("\\", "/").strip()
    s_l = s_norm.lower()
    for loc, can in _alias_map():
        if s_l == loc:
            return can.replace("/", "\\")
        if s_l.startswith(loc + "/"):
            return (can + s_norm[len(loc):]).replace("/", "\\")
    return s


def _get_docling_converter():
    """Lazily construct the Docling converter (imports docling only when needed).

    OCR is disabled and the fast PyPdfium backend is used: the Investment Team
    KB is overwhelmingly born-digital text PDFs/reports, so native text
    extraction is both accurate and 20-50x faster than running OCR on every
    page. Image-only scans are the rare exception and simply yield less text.
    """
    global _DOC_CONVERTER
    if _DOC_CONVERTER is None:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        _pipeline = PdfPipelineOptions()
        _pipeline.do_ocr = False  # born-digital text PDFs don't need OCR

        _format_options = {
            InputFormat.PDF: PdfFormatOption(
                backend=PyPdfiumDocumentBackend,
                pipeline_options=_pipeline,
            )
        }
        _DOC_CONVERTER = DocumentConverter(format_options=_format_options)
    return _DOC_CONVERTER


# ---------------------------------------------------------------------------
# Fast, model-free text extractors (no Docling / no HuggingFace models).
# Used for the bulk KB ingest so 5,000+ files finish in minutes instead of
# hours, and so ingestion never depends on the fragile Docling model cache.
# Docling is kept only as a last-resort fallback in load_text().
# ---------------------------------------------------------------------------

def _extract_pdf(path: Path) -> str:
    """Extract text from a PDF using pypdfium2 (fast, no OCR/models)."""
    return "\n\n".join(t for _page, t in _iter_pdf_pages(path))


def _iter_pdf_pages(path: Path):
    """Yield ``(1-based page_number, text)`` for pages that have extractable text."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            tp = page.get_textpage()
            text = tp.get_text_range().strip()
            if text:
                yield i + 1, text
    finally:
        pdf.close()


def _chunks_with_pages(path: Path) -> list[tuple[str, int | None]]:
    """Return ``(chunk_text, page_or_None)`` pairs for ingest metadata.

    PDFs are chunked **per page** so each vector can carry a jump-to page.
    Other formats keep the existing whole-document chunker (page=None).
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        out: list[tuple[str, int | None]] = []
        for page_num, page_text in _iter_pdf_pages(path):
            for ch in chunk_text(page_text):
                out.append((ch, page_num))
        return out
    if suffix in config.SPREADSHEET_SUFFIXES:
        out = []
        for rt in _xlsx_row_texts(path):
            for ch in chunk_text(rt):
                out.append((ch, None))
        return out
    text = load_text(path)
    return [(ch, None) for ch in chunk_text(text)]


def _extract_docx(path: Path) -> str:
    """Extract paragraphs + table cells from a .docx (python-docx)."""
    import docx

    doc = docx.Document(str(path))
    parts: list[str] = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def _extract_pptx(path: Path) -> str:
    """Extract text from slides + tables of a .pptx (python-pptx)."""
    import pptx

    prs = pptx.Presentation(str(path))
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    txt = "".join(r.text for r in para.runs).strip()
                    if txt:
                        parts.append(txt)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def _extract_html(path: Path) -> str:
    """Lightweight tag-strip for HTML (no extra deps)."""
    import re

    text = path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_xlsx(path: Path) -> str:
    """Extract text from an Excel workbook as readable Markdown tables.

    Each sheet becomes a section with the sheet name as a heading, followed
    by a Markdown table of non-empty rows. Empty rows/columns are dropped so
    the embedding chunks contain real content instead of whitespace noise.
    """
    import pandas as pd

    sheets = pd.read_excel(str(path), sheet_name=None, engine="openpyxl")
    parts: list[str] = []
    for sheet_name, df in sheets.items():
        # Drop fully empty rows and columns, coerce everything to string.
        df = df.dropna(how="all").dropna(axis=1, how="all").astype(str)
        if df.empty:
            continue
        # Avoid long identical replacements of "nan" literals from empty cells.
        df = df.replace(r"^nan$", "", regex=True)
        parts.append(f"## Sheet: {sheet_name}\n")
        parts.append(df.to_markdown(index=False))
    return "\n\n".join(parts)


def _xlsx_row_texts(path: Path, max_cell: int = 32000) -> list[str]:
    """Yield ONE full-text string per spreadsheet row (no markdown table).

    Each row becomes a single coherent record (title + FULL content + metadata)
    so the generic paragraph/char chunker can split long articles into proper
    overlapping ~CHUNK_SIZE pieces. 

    Why not a markdown table or per-row truncation:
      * Dumping the sheet via ``df.to_markdown`` explodes -- it pads every cell
        in a column to the widest entry, turning a 639-row news feed with 32KB
        ``content`` cells into ~300MB of padded whitespace that never finishes
        embedding.
      * Truncating each row to one 800-char chunk destroys long articles
        (a ``content`` cell can hold ~8,000 words). Keeping the FULL cell text
        and letting ``chunk_text`` bound the final slice count preserves the
        whole article while staying embeddable (≈ 15-25k chunks for this file).
    Excel already caps a cell at 32,767 chars, so ``max_cell`` is only a safety
    rail against non-Excel sources.
    """
    import pandas as pd

    sheets = pd.read_excel(str(path), sheet_name=None, engine="openpyxl")
    out: list[str] = []
    for sheet_name, df in sheets.items():
        df = df.dropna(how="all").dropna(axis=1, how="all").astype(str)
        if df.empty:
            continue
        df = df.replace(r"^nan$", "", regex=True)
        # Use POSITIONAL access: column headers can be non-string values
        # (e.g. Excel dates like 2024-03-01) whose stringified form is not a
        # valid key on the row Series.
        cols = [str(c) for c in df.columns]
        for _, row in df.iterrows():
            parts = []
            for ci, c in enumerate(cols):
                v = str(row.iloc[ci])
                if len(v) > max_cell:
                    v = v[:max_cell].rstrip() + "\u2026"
                if v.strip():
                    parts.append(f"{c}: {v}")
            if parts:
                out.append(f"[Sheet {sheet_name}] " + " | ".join(parts))
    return out


def load_text(path: Path) -> str:
    """Load a file and return its plain text content.

    Fast, model-free extractors handle PDF/DOCX/PPTX/HTML/XLSX. Docling is only
    a fallback for anything the fast path cannot read, so bulk ingestion is
    reliable and fast even without the HuggingFace model cache.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in config.NATIVE_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pptx":
        return _extract_pptx(path)
    if suffix == ".html":
        return _extract_html(path)
    if suffix in config.SPREADSHEET_SUFFIXES:
        return _extract_xlsx(path)
    # Fallback to Docling for any other supported type.
    if suffix in config.DOCLING_SUFFIXES:
        try:
            converter = _get_docling_converter()
            result = converter.convert(str(path))
            return result.document.export_to_markdown()
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Docling failed on {path.name}: {e}") from e
    raise ValueError(f"Unsupported file type: {suffix!r} ({path.name})")


def _stable_doc_id(source_path: Path) -> str:
    """Deterministic doc_id from the CANONICAL resolved source path.

    Canonicalization (see canonical_source) keeps the id stable whether the
    file is ingested via UNC/G: or via the OneDrive local folder, so switching
    the sync root never duplicates the corpus.
    """
    key = canonical_source(Path(source_path).resolve())
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _file_content_hash(path: Path) -> str:
    """SHA-256 of the file's raw bytes (streamed, memory-safe).

    This is the change-detection key. ``doc_id`` is path-based and cannot tell
    that an already-ingested file was edited in place, so without this an edit
    would silently keep serving stale vectors. Hashing the bytes catches edits.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def chunk_text(
    text: str,
    chunk_size: int = config.CHUNK_SIZE,
    overlap: int = config.CHUNK_OVERLAP,
) -> list[str]:
    """Paragraph-aware chunker with overlap.

    Paragraphs are packed greedily up to chunk_size; oversized paragraphs are
    hard-split. A tail of `overlap` characters from the previous chunk is
    prepended to each subsequent chunk to preserve context across boundaries.
    """
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 1 <= chunk_size:
            current = (current + "\n\n" + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            if len(para) > chunk_size:
                # Hard-split a paragraph longer than the window.
                step = max(1, chunk_size - overlap)
                for i in range(0, len(para), step):
                    chunks.append(para[i : i + chunk_size])
                current = ""
            else:
                current = para
    if current:
        chunks.append(current)

    # Apply overlap between consecutive chunks.
    if overlap > 0 and len(chunks) > 1:
        overlapped: list[str] = [chunks[0]]
        for i, c in enumerate(chunks[1:], start=1):
            prev = chunks[i - 1]
            tail = prev[-overlap:] if len(prev) > overlap else prev
            overlapped.append((tail + "\n\n" + c).strip())
        chunks = overlapped

    return chunks


def _sync_doc_hash(doc_id: str, chunk0_meta: dict, content_hash: str) -> None:
    """Metadata-only update: rewrite ``content_hash`` on every row of ``doc_id``.

    Used by preserve-mode ingestion when the sync root moves (e.g. G: -> the
    OneDrive local folder). The new root's bytes often differ from what was
    originally ingested even when the CONTENT is identical, so without this the
    next auto-sync tick would compare hashes, see "changed" for every file and
    mass-re-embed the whole corpus. Patching just the hash (no vectors, no
    re-embedding) makes future ticks see "unchanged".
    """
    from rag.store_lancedb import _open_table

    new_meta = dict(chunk0_meta or {})
    new_meta["content_hash"] = content_hash
    _open_table().update(
        where=f"id LIKE '{doc_id}%'",
        values={"metadata": json.dumps(new_meta, ensure_ascii=False)},
    )


def ingest_file(
    path,
    user_id: str = config.PLACEHOLDER_USER_ID,
    scope: str = config.PLACEHOLDER_SCOPE,
    force: bool = False,
    doc_date: str | None = None,
    respect_exclusions: bool = True,
    preserve_existing: bool = False,
) -> int:
    """Ingest a single file. Returns the number of NEW/changed chunks upserted.

    Change detection is content-hash based (not path based):
      * New file (doc_id_0 not in store) -> embed, store content_hash + chunk_count.
      * Existing file with a stored content_hash:
          - hash matches -> return 0 (unchanged, skip — cheap, no re-embed).
          - hash differs  -> delete the previous chunks, then re-embed (captures
            in-place edits that a path-based doc_id alone would miss).
      * Existing file WITHOUT a stored content_hash (legacy rows written before
        this feature) -> treated as "already correct, skip". This keeps the first
        sync after deployment cheap: it does NOT mass re-embed the whole KB.
        Run ``python -m rag.ingest --backfill`` once to add hashes to legacy
        rows so edits to them are also caught (model-free, no re-embedding).

    ``preserve_existing=True`` (used when migrating the sync root): an existing
    doc is NEVER re-embedded and its stored hash is left untouched (hash
    alignment for the migrated corpus is done by a separate batched pass —
    a per-doc tbl.update on a 700k-row table rewrites the whole dataset and is
    far too slow per file). Genuinely new files are still embedded.
    """
    path = Path(path)
    doc_id = _stable_doc_id(path)
    collection = get_collection()
    existing = collection.get(ids=[f"{doc_id}_0"], include=[])
    if existing["ids"] and hasattr(collection, "get_metadatas"):
        metas = collection.get_metadatas([f"{doc_id}_0"])
        meta = metas[0] if metas else None
        stored_hash = (meta or {}).get("content_hash")
        if stored_hash is None:
            # Legacy row, no hash recorded yet — assume unchanged, skip.
            return 0
        if preserve_existing:
            # Migration mode: keep existing vectors untouched and do NOT touch
            # the stored hash either (see docstring). Skip WITHOUT reading the
            # file bytes — hashing ~6k legacy files from the new root is pure
            # waste when we are not going to re-embed them.
            return 0
        # Normal mode: change detection requires the CURRENT bytes.
        content_hash = _file_content_hash(path)
        if respect_exclusions and is_excluded(content_hash):
            return 0
        if stored_hash == content_hash and not force:
            return 0  # unchanged
        # Content changed (or forced): drop the previous chunks first so no
        # stale vectors from the old version linger.
        old_count = (meta or {}).get("chunk_count", 0)
        if old_count:
            old_ids = [f"{doc_id}_{i}" for i in range(old_count)]
            collection.delete(ids=old_ids)
        elif hasattr(collection, "delete_by_doc_id"):
            collection.delete_by_doc_id(doc_id)
    else:
        # New file (or a store without metadata lookup): full ingest.
        content_hash = _file_content_hash(path)
        if respect_exclusions and is_excluded(content_hash):
            return 0
    # Spreadsheets / PDF / others: see ``_chunks_with_pages`` (PDF chunks carry
    # a 1-based page so the UI can jump to evidence).
    paired = _chunks_with_pages(path)
    chunks = [c for c, _p in paired]

    # A file with no extractable text (image-only scan, empty sheet, etc.)
    # produces zero chunks — do not call the embedding model with an empty
    # batch (raises "Expected Embeddings to be non-empty").
    if not chunks:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    ids, documents, metadatas = [], [], []
    for i, (chunk, page) in enumerate(paired):
        ids.append(f"{doc_id}_{i}")
        documents.append(chunk)
        meta = {
            "user_id": user_id,
            "scope": scope,
            "source": canonical_source(path.resolve()),
            "doc_id": doc_id,
            "chunk_index": i,
            "created_at": now,
            "email_date": doc_date,
            "content_hash": content_hash,
            "chunk_count": len(chunks),
        }
        if page is not None:
            meta["page"] = int(page)
        metadatas.append(meta)

    # Upsert: re-running uses stable ids, so no duplicate spam.
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
    return len(chunks)


# ---------------------------------------------------------------------------
# Pruning: keep the store in sync with the filesystem.
#
# Ingestion is add-only + idempotent-skip (see ingest_file), so when a source
# file is DELETED its vectors stay in the store as orphans -- RAG still returns
# them, which is both stale and a privacy leak. prune_missing() removes rows
# whose `source` path no longer exists.
#
# PERF (why this used to hang): the original code called os.path.exists() once
# per stored vector (~395k SMB round-trips on the G: network share) at the
# START of every ingest_directory() call. That blocked new-file ingestion
# entirely. The fixed version (see prune_missing) does a SINGLE os.walk() of the
# KB root and compares in memory. Pruning is now DECOUPLED from the per-tick
# ingest: ingest_directory(run_prune=False) only embeds new/changed files, and
# prune_missing() runs on its own slow schedule (config.AUTO_PRUNE_INTERVAL_SECONDS).
# ---------------------------------------------------------------------------

def _collect_existing_paths(root: Path) -> set:
    """One network traversal: return the set of normalized existing file paths.

    A single os.walk() over the share is vastly cheaper than checking 395k
    stored vectors individually (which does 395k SMB round-trips and hangs).
    Paths are normalized with os.path.normcase so the comparison is
    case-insensitive on Windows, and canonicalized (see canonical_source) so a
    walk rooted at the OneDrive local folder still matches UNC-stored sources.
    Returns an empty set if the root is missing.
    """
    existing = set()
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                existing.add(os.path.normcase(os.path.normpath(canonical_source(p))))
    except Exception:  # noqa: BLE001 -- unmounted share etc. -> treat as empty
        pass
    return existing


def _all_store_source_rows():
    """Return [(id, source), ...] for every row in the active store.

    Reads via the vector-search full-scan path (nprobes covering all IVF
    partitions) because the `lance` python package is not bundled, so
    ``to_lance()`` is unavailable. The embedding function is already loaded by
    the caller (ingest loads it via upsert), so this stays cheap.
    """
    from rag.store_lancedb import _open_table, _get_ef

    tbl = _open_table()
    total = tbl.count_rows()
    if total == 0:
        return []
    vec = _get_ef()(["__prune_probe__"])[0]
    rows = tbl.search(vec).nprobes(256).limit(total).select(["id", "metadata"]).to_list()
    out = []
    for r in rows:
        m = r.get("metadata")
        if not m:
            continue
        try:
            meta = json.loads(m)
        except Exception:
            continue
        src = meta.get("source")
        if src:
            out.append((r["id"], src))
    return out


def prune_missing(dry_run: bool = False, root: Path | None = None,
                  missing_ratio_abort: float = 0.5) -> dict:
    """Delete store rows whose ``source`` file no longer exists on disk.

    Safety rail: if more than ``missing_ratio_abort`` of distinct sources are
    missing we ABORT and delete nothing. That pattern means the share/disk is
    simply unmounted (not real deletions) -- blindly deleting would wipe the KB.
    This is critical because source paths are UNC (``\\\\server\\...``) and a
    colleague running this without the share mounted would otherwise nuke
    everything.

    Returns a summary dict. With ``dry_run=True`` nothing is deleted.

    ``root``: if provided, prune walks it ONCE to build the existing-file set
    (cheap on a network share). If None, every source is checked with
    os.path.exists (slow but correct -- used by the manual CLI).
    """
    rows = _all_store_source_rows()
    by_source: dict[str, list[str]] = {}
    for rid, src in rows:
        by_source.setdefault(src, []).append(rid)

    distinct = len(by_source)
    # Build the existing-file set with a SINGLE walk of the share (cheap even on
    # SMB). Sources under `root` are checked via set membership; sources outside
    # root (e.g. uploaded temp files) fall back to per-file os.path.exists.
    existing = _collect_existing_paths(Path(root)) if root else set()

    def _is_missing(src: str) -> bool:
        if existing and os.path.normcase(os.path.normpath(canonical_source(src))) in existing:
            return False
        return not os.path.exists(src)

    missing = {src: ids for src, ids in by_source.items() if _is_missing(src)}
    if distinct and len(missing) / distinct > missing_ratio_abort:
        return {
            "aborted": True,
            "reason": (
                f"{len(missing)}/{distinct} sources missing "
                f"(> {missing_ratio_abort:.0%}) -- share/disk likely unmounted; "
                f"refusing to delete."
            ),
            "deleted_rows": 0,
            "deleted_sources": 0,
            "sources": {},
        }

    to_delete = [rid for ids in missing.values() for rid in ids]
    summary = {
        "aborted": False,
        "dry_run": dry_run,
        "distinct_sources": distinct,
        "missing_sources": len(missing),
        "deleted_rows": 0 if dry_run else len(to_delete),
        "sources": {src: len(ids) for src, ids in missing.items()},
    }
    if dry_run or not to_delete:
        return summary

    from rag.store_lancedb import _open_table

    tbl = _open_table()
    batch_size = 5000
    for i in range(0, len(to_delete), batch_size):
        chunk = to_delete[i : i + batch_size]
        id_list = ", ".join("'" + str(x).replace("'", "''") + "'" for x in chunk)
        tbl.delete(where=f"id IN ({id_list})")
    return summary


def ingest_directory(
    dir,
    user_id: str = config.PLACEHOLDER_USER_ID,
    scope: str = config.PLACEHOLDER_SCOPE,
    on_progress=None,
    run_prune: bool = False,
    respect_exclusions: bool = True,
    preserve_existing: bool = False,
) -> dict:
    """Recursively ingest every supported file under ``dir``.

    Returns a summary dict:
        {"files": int, "chunks": int, "skipped": [str], "errors": [str]}

    - Descends into sub-folders (the whole Investment Team tree).
    - Skips system/build dirs (INGEST_SKIP_DIRS) and Office/OS temp-lock
      files (``~$...``, ``.tmp``, hidden ``.`` files).
    - A failure on one file does NOT abort the run — it is recorded under
      ``errors`` and ingestion continues (SMB files can be locked by others).
    - Stable doc_ids make re-running idempotent (no duplicate chunks).

    ``on_progress(done, total, current_name, summary)`` is called after each
    file when provided (used by a future streaming UI).
    """
    dir = Path(dir)
    # Collect targets first so we can report total progress.
    targets: list[Path] = []
    for root, dirs, files in os.walk(dir):
        # Prune skipped dirs in place so os.walk never descends into them.
        dirs[:] = [
            d for d in dirs
            if d not in INGEST_SKIP_DIRS and not d.startswith(".")
        ]
        for f in files:
            if f.startswith(_INGEST_TEMP_PREFIXES):
                continue
            p = Path(root) / f
            if p.suffix.lower() not in config.SUPPORTED_SUFFIXES:
                continue
            # Skip files inside user-excluded folders (e.g. a folder removed
            # from the DB) so they never re-appear during auto-sync/manual ingest.
            if respect_exclusions:
                try:
                    rel = p.relative_to(dir).as_posix()
                except Exception:
                    rel = ""
                if rel and is_folder_excluded(rel):
                    continue
            targets.append(p)

    total = len(targets)
    result: dict = {"files": 0, "chunks": 0, "skipped": [], "errors": []}
    # Deletion cleanup is OPT-IN (run_prune). By default ingest_directory() only
    # embeds NEW / CHANGED files so a sync tick never blocks on a full-share
    # filesystem walk. Pruning runs on its own slow schedule (see the auto-sync
    # loop / config.AUTO_PRUNE_INTERVAL_SECONDS) or on an explicit manual
    # "Ingest KB" click (run_prune=True). When enabled, walk `dir` once so the
    # check is cheap even on the G: network share.
    if run_prune:
        try:
            result["pruned"] = prune_missing(dry_run=False, root=dir)
        except Exception as e:  # noqa: BLE001 -- never let pruning break an ingest run
            result["pruned"] = {
                "aborted": True,
                "reason": f"prune error: {e}",
                "deleted_rows": 0,
                "deleted_sources": 0,
                "sources": {},
            }
    for i, p in enumerate(targets, 1):
        try:
            n = ingest_file(p, user_id=user_id, scope=scope,
                            respect_exclusions=respect_exclusions,
                            preserve_existing=preserve_existing)
            result["files"] += 1
            result["chunks"] += n
        except Exception as e:  # noqa: BLE001 — one bad file must not abort the run
            result["errors"].append(f"{p.name}: {e}")
        if on_progress:
            try:
                on_progress(i, total, p.name, result)
            except Exception:
                pass
    return result


def backfill_hashes(dry_run: bool = False) -> dict:
    """Add ``content_hash`` + ``chunk_count`` to legacy rows that lack them.

    One-time, MODEL-FREE: it reads each stored row's vector/document and rewrites
    only its metadata, computing the hash from the source file on disk. This
    enables edit detection for documents ingested before this feature existed,
    without re-running the embedding model. Rows whose source file is missing
    are left untouched (prune_missing handles deletions). Only chunk-0 rows
    carry the change-detection metadata, so the scan skips the rest. Iterate in
    Arrow batches to stay memory-bounded over ~400k rows.
    """
    from rag.store_lancedb import _open_table

    tbl = _open_table()
    ds = tbl.to_lance()
    scanner = ds.scanner(columns=["id", "vector", "document", "metadata"])
    updated = skipped = missing = 0
    batch_updates: list[dict] = []
    for batch in scanner.to_batches(batch_size=4096):
        cols = batch.to_pydict()
        ids = cols["id"]
        vectors = cols["vector"]
        docs = cols["document"]
        metas = cols["metadata"]
        for i in range(len(ids)):
            rid = ids[i]
            if not str(rid).endswith("_0"):
                continue  # only chunk 0 carries change-detection metadata
            meta_raw = metas[i]
            meta = json.loads(meta_raw) if meta_raw else {}
            if meta.get("content_hash") and meta.get("chunk_count"):
                skipped += 1
                continue
            src = meta.get("source")
            if not src or not os.path.exists(src):
                missing += 1
                continue
            try:
                ch = _file_content_hash(Path(src))
                cnt = len(chunk_text(load_text(Path(src))))
            except Exception:
                missing += 1
                continue
            meta["content_hash"] = ch
            meta["chunk_count"] = cnt
            batch_updates.append(
                {
                    "id": rid,
                    "vector": vectors[i],
                    "document": docs[i],
                    "metadata": json.dumps(meta, ensure_ascii=False),
                }
            )
            updated += 1
    if not dry_run and batch_updates:
        # Upsert in batches to avoid one giant transaction.
        for i in range(0, len(batch_updates), 2000):
            tbl.upsert(batch_updates[i : i + 2000])
    return {
        "updated": updated,
        "skipped": skipped,
        "missing_source": missing,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="QP RAG ingestion / pruning")
    ap.add_argument("--prune", action="store_true", help="prune rows whose source file is missing")
    ap.add_argument("--backfill", action="store_true",
                    help="add content_hash+chunk_count to legacy rows (no re-embed)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --prune/--backfill: report only, do not mutate")
    args = ap.parse_args()
    if args.prune:
        # Resolve the KB root (mirrors rag_service._kb_root) so prune does a
        # single cheap walk instead of 395k per-vector network checks.
        root = None
        try:
            for cand in (
                Path(__file__).resolve().parent.parent.parent / "qp_email_helper.config.json",
                Path("qp_email_helper.config.json"),
            ):
                if cand.exists():
                    d = json.loads(cand.read_text(encoding="utf-8"))
                    r = (d.get("shared_root") or "").strip()
                    if r:
                        root = Path(r)
                    break
        except Exception:
            root = None
        out = prune_missing(dry_run=args.dry_run, root=root)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.backfill:
        out = backfill_hashes(dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        ap.print_help()
