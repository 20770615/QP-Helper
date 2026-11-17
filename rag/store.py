"""Vector store factory (LanceDB / Qdrant / Chroma).

LanceDB is the default on macOS and Windows for large KBs. Chroma is only
imported when ``RAG_VECTOR_BACKEND=chroma`` so Mac setups can omit chromadb.
"""

from __future__ import annotations

import os

from rag import config
from rag.embeddings import get_embedding_function

_CLIENT = None


def get_client():
    """Return a memoized Chroma persistent client rooted at config.PERSIST_DIR."""
    global _CLIENT
    if _CLIENT is None:
        # Disable Chroma telemetry before importing chromadb.
        os.environ["CHROMA_TELEMETRY"] = "false"
        import chromadb  # local import keeps LanceDB-only installs light

        config.PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(
            path=str(config.PERSIST_DIR),
            settings=chromadb.config.Settings(anonymized_telemetry=False),
        )
    return _CLIENT


def get_collection():
    """Get-or-create the RAG collection in cosine space.

    Returns LanceDB / Qdrant / Chroma depending on ``config.VECTOR_BACKEND``.
    """
    if config.VECTOR_BACKEND == "lancedb":
        from rag.store_lancedb import LanceDBCollection

        return LanceDBCollection()
    if config.VECTOR_BACKEND == "qdrant":
        from rag.store_qdrant import get_qdrant_collection

        return get_qdrant_collection()
    client = get_client()
    return client.get_or_create_collection(
        name=config.COLLECTION_NAME,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )
