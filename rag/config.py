"""Project configuration: paths, collection name, and chunking parameters.

All paths are resolved relative to this file so the package stays portable
across company workstation PCs (no shared server — runs as a local service).
"""

import os
from pathlib import Path

# --- Locations (all resolved relative to this file so the package is portable) ---
# rag/ -> parent is the project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PERSIST_DIR = PROJECT_ROOT / "chroma_store"   # Chroma on-disk vector store
SAMPLES_DIR = PROJECT_ROOT / "samples"         # demo documents to ingest

# --- Vector store backend -------------------------------------------------
# "chroma"  : original in-memory-backed store (kept for rollback only).
# "lancedb" : disk-backed, low-RAM store (RECOMMENDED for large KBs). The ~3GB
#             of vectors live on SSD (columnar Lance format) and are read on
#             demand, so the old machine stops choking. Handles ~400k points
#             with no server. Run migrate_to_lancedb.py ONCE before switching.
# "qdrant"  : alternative disk-backed store (local embedded mode — not advised
#             past ~20k points, OOMs on the old box during bulk index build).
# Override without editing code via env: RAG_VECTOR_BACKEND=lancedb
VECTOR_BACKEND = os.environ.get("RAG_VECTOR_BACKEND", "lancedb").strip().lower()
QDRANT_DIR = PROJECT_ROOT / "qdrant_store"      # Qdrant on-disk store (unused by default)
LANCEDB_DIR = PROJECT_ROOT / "lancedb_store"    # LanceDB on-disk store (recommended)

# --- Collection / chunking ---
COLLECTION_NAME = "quam_rag"
CHUNK_SIZE = 800        # target characters per chunk
CHUNK_OVERLAP = 120     # overlap between consecutive chunks

# --- Embedding model (single swap point for retrieval quality) ---
# 384-dim multilingual model: bridges zh queries -> en KB. Same dimension as the
# old all-MiniLM-L6-v2, so no Chroma collection schema change is needed (only a
# re-embed). Upgrade: "BAAI/bge-m3" (1024-dim, stronger cross-lingual) ->
# recreate the collection (delete chroma_store and re-ingest).
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

# --- Metadata placeholders (authentication is Approach B; schema built day one) ---
PLACEHOLDER_USER_ID = "shared"
PLACEHOLDER_SCOPE = "shared"

# --- Supported file types ---
NATIVE_SUFFIXES = {".txt", ".md", ".csv"}                       # read natively
SPREADSHEET_SUFFIXES = {".xlsx"}                                 # parse via pandas/openpyxl
DOCLING_SUFFIXES = {".pdf", ".docx", ".pptx", ".html"}         # parse via Docling
SUPPORTED_SUFFIXES = NATIVE_SUFFIXES | SPREADSHEET_SUFFIXES | DOCLING_SUFFIXES

# --- Auto-sync poller (RAG service background thread) ----------------------
# Minutes between automatic KB re-ingestion ticks performed by rag_service.
# Each tick re-crawls the shared root and embeds NEW / CHANGED files only
# (fast, no full-store filesystem walk). Deletion cleanup (prune_missing) is
# NOT done on every tick -- see AUTO_PRUNE_INTERVAL_SECONDS. 0 disables the
# poller. Keep this in the 5-10 min range -- every tick loads the ~1GB
# embedding model, so a short interval needlessly keeps the model resident.
# Override with the RAG_AUTO_SYNC_MINUTES environment variable.
AUTO_SYNC_INTERVAL_MINUTES = int(os.environ.get("RAG_AUTO_SYNC_MINUTES", "10"))

# Seconds between deletion-cleanup (prune_missing) runs. Kept SEPARATE and much
# slower than the sync tick on purpose: on a network share (G: SMB) checking
# 395k stored vectors one-by-one with os.path.exists hangs, which used to block
# new-file ingestion entirely. prune_missing now does a SINGLE os.walk of the
# share and compares in memory, so a daily run is cheap and safe. Override with
# the RAG_AUTO_PRUNE_SECONDS environment variable (0 = never auto-prune).
AUTO_PRUNE_INTERVAL_SECONDS = int(os.environ.get("RAG_AUTO_PRUNE_SECONDS", str(24 * 60 * 60)))
