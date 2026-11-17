"""Qdrant disk-backed vector store (local embedded mode, no external server).

Drop-in alternative to the Chroma store in ``rag/store.py``. It mirrors the
Chroma collection surface that ``rag/retrieve`` and ``rag/ingest`` actually use:

    collection.query(query_texts=[...], n_results=n, where=...) -> dict
    collection.upsert(ids=..., documents=..., metadatas=...)      -> None
    collection.get(ids=..., include=[])                           -> dict

So ``retrieve.py`` / ``ingest.py`` / ``main.py`` need NO changes.

Why this fixes the lag
----------------------
Chroma keeps the full ~3GB of vectors resident in RAM. Qdrant keeps only a
small HNSW navigation graph in RAM and stores the vectors on disk
(``on_disk=True``); at query time it reads only the few relevant vectors from
the SSD. RAM drops from ~3GB to a few hundred MB, so the machine stops lagging
and retrieval stops timing out.

Embeddings are computed by the same model as before (``rag.embeddings``), so
query/ingest behavior is identical — only the storage backend changes.
"""

from __future__ import annotations

import uuid

from rag import config
from rag.embeddings import get_embedding_function

# Cache the embedding function so the (large) sentence-transformers model loads
# only once per process.
_EF = None


def _get_ef():
    global _EF
    if _EF is None:
        _EF = get_embedding_function()
    return _EF


# Qdrant's local (embedded) mode requires point IDs to be valid UUIDs (or ints).
# Chroma ids are arbitrary strings ("<sha256>_<n>"), so we hash them
# deterministically to a UUID. The original id is kept in payload["_cid"] so
# retrieve.py / harness.py still see the familiar id strings.
_ID_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf18fc0a1e")


def _to_qid(chroma_id: str) -> str:
    return str(uuid.uuid5(_ID_NS, str(chroma_id)))


def _chroma_where_to_qdrant(where):
    """Translate a Chroma ``where`` filter into a Qdrant ``Filter``.

    Supports the only two shapes ``retrieve.py`` ever emits: a flat equality
    dict (e.g. ``{"scope": "shared"}``) or ``{"$and": [cond, ...]}``.
    Returns ``None`` when there is no filter.
    """
    if not where:
        return None
    from qdrant_client import models

    def _cond(c):
        key = next(iter(c))
        return models.FieldCondition(key=key, match=models.MatchValue(value=c[key]))

    if "$and" in where:
        must = [_cond(c) for c in where["$and"]]
    else:
        must = [_cond(where)]
    return models.Filter(must=must)


class QdrantCollection:
    """Chroma-shaped wrapper around a local (embedded) Qdrant collection."""

    def __init__(self):
        from qdrant_client import QdrantClient, models

        self._client = QdrantClient(path=str(config.QDRANT_DIR))
        self._name = config.COLLECTION_NAME
        # Probe the embedding dimension from one text so we never hardcode it.
        self._dim = len(_get_ef()(["__dim_probe__"])[0])
        if not self._client.collection_exists(self._name):
            self._client.create_collection(
                collection_name=self._name,
                vectors_config=models.VectorParams(
                    size=self._dim,
                    distance=models.Distance.COSINE,
                    on_disk=True,  # vectors on disk -> not resident in RAM
                ),
            )

    def upsert(self, ids, documents, metadatas):
        from qdrant_client import models

        vectors = _get_ef()(list(documents))
        points = [
            models.PointStruct(
                id=_to_qid(i),
                vector=v,
                payload={"document": doc, "_cid": str(i), **(meta or {})},
            )
            for i, v, doc, meta in zip(ids, vectors, documents, metadatas)
        ]
        self._client.upsert(collection_name=self._name, points=points)

    def add(self, ids, documents, metadatas):
        # Chroma's add and upsert behave identically for our idempotent ingest.
        self.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def get(self, ids, include=None):
        recs = self._client.retrieve(
            collection_name=self._name,
            ids=[_to_qid(i) for i in ids],
            with_payload=True,
            with_vectors=False,
        )
        # Return the original Chroma id (payload["_cid"]) so callers see the
        # same strings they got from Chroma. Chroma returns these keys (None
        # when include=[]); ingest only reads "ids".
        found = [(r.payload or {}).get("_cid") for r in recs]
        return {"ids": found, "documents": None, "metadatas": None, "embeddings": None}

    def query(self, query_texts, n_results=5, where=None):
        q = _get_ef()(list(query_texts))[0]
        qfilter = _chroma_where_to_qdrant(where)
        resp = self._client.query_points(
            collection_name=self._name,
            query=q,
            limit=n_results,
            query_filter=qfilter,
            with_payload=True,
            with_vectors=False,
        )
        points = resp.points
        ids = [[(p.payload or {}).get("_cid") for p in points]]
        documents = [[(p.payload or {}).get("document") for p in points]]
        metadatas = [
            [
                {k: v for k, v in (p.payload or {}).items() if k not in ("document", "_cid")}
                for p in points
            ]
        ]
        # Chroma reports cosine *distance* (1 - similarity); Qdrant returns similarity.
        distances = [[(1.0 - p.score) for p in points]]
        return {
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas,
            "distances": distances,
        }

    def count(self):
        return self._client.count(collection_name=self._name).count

    def delete(self, ids=None, where=None):
        from qdrant_client import models

        if ids:
            self._client.delete(
                collection_name=self._name,
                points_selector=models.PointIdsSelector(points=[_to_qid(i) for i in ids]),
            )
        elif where:
            self._client.delete(
                collection_name=self._name,
                points_selector=models.FilterSelector(filter=_chroma_where_to_qdrant(where)),
            )


_QD_COLLECTION = None


def get_qdrant_collection():
    """Return a memoized QdrantCollection wrapper."""
    global _QD_COLLECTION
    if _QD_COLLECTION is None:
        _QD_COLLECTION = QdrantCollection()
    return _QD_COLLECTION
