"""Disk-backed vector store using LanceDB (embedded, no server).

This is a drop-in replacement for the Chroma-backed collection used by
``rag/store.py``. It exposes the *same* surface the rest of the app expects:

    collection.add(ids, documents, metadatas)
    collection.upsert(ids, documents, metadatas)
    collection.get(ids, include=[...])
    collection.query(query_texts, n_results=5, where=None)
    collection.count()
    collection.delete(ids=None, where=None)

Why LanceDB here (instead of Chroma / Qdrant-local):
  * The corpus is ~396k points / ~3GB. Chroma keeps the whole index resident
    in RAM -> the old machine lags and retrieval times out.
  * Qdrant's *local embedded* mode is not recommended past 20k points and OOMs
    on a memory-starved box during bulk index build.
  * LanceDB stores vectors on disk (SSD) in a columnar format and reads only
    the shards a query needs. RAM stays at the working set (a few hundred MB),
    so both lag modes disappear. No server, no Docker, no cloud.

The embedding function is attached here only -- the single place that knows
about the embedding backend. Migration copies already-computed vectors directly
(see migrate_to_lancedb.py), so no re-embedding / API cost is involved.
"""
from __future__ import annotations

import json
import os
import re

import pyarrow as pa
import pyarrow.compute as pc

from rag import config
from rag.embeddings import get_embedding_function
from rag._pathmatch import source_matches  # shared @Ask-about-folder/file matcher

import lancedb

# Cache of folder/file -> matching row IDs (a pyarrow Array per filter tuple).
# The table is read-only during serving (ingestion is a separate process), so a
# cached ID set stays valid for the life of the service process. Keyed by the
# sorted filter tuple: a repeated @Ask-about-folder question costs one
# (id, metadata)-only scan instead of one per turn.
_FOLDER_IDS_CACHE: dict[tuple, pa.Array] = {}

# Cache the embedding function so the (large) sentence-transformers model loads
# only once per process.
_EF = None

# Metadata keys that are ALSO stored as real LanceDB scalar columns (so they can
# be SQL-filtered at query time). The production LanceDB table (437k rows) was
# created WITHOUT the `email_date` scalar column, and LanceDB's add_columns
# migration fails silently on it — so referencing `email_date` in a `.select()`
# or `.where()` throws "No field named email_date". To stay safe and portable
# across tables, `email_date` is matched in Python against the JSON `metadata`
# column (where it is always present), exactly like `scope`/`user_id`. Hence
# this set is empty: no key is SQL-filtered, all filtering is Python-side.
_COLUMN_KEYS = set()


def _get_ef():
    global _EF
    if _EF is None:
        _EF = get_embedding_function()
    return _EF


def _column_predicate(where):
    """Build a LanceDB SQL predicate for keys that map to real scalar columns.

    Recurses through Chroma-style ``$and`` / ``$or`` wrappers (``retrieve``
    wraps multiple conditions in ``$and``), so a column key nested inside
    ``$and`` is still turned into a SQL predicate. Returns None when no
    column-key is present, so callers can skip ``.where()``. Keys that live
    only inside the JSON ``metadata`` column (scope, user_id) are excluded —
    they are matched in Python via _where_match().
    """
    if not where:
        return None
    out: list[str] = []
    _collect_column_conditions(where, out)
    return " AND ".join(out) if out else None


def _collect_column_conditions(node, out: list[str]) -> None:
    if not isinstance(node, dict):
        return
    if "$and" in node:
        for sub in node["$and"]:
            _collect_column_conditions(sub, out)
    elif "$or" in node:
        or_parts = []
        for sub in node["$or"]:
            sub_out: list[str] = []
            _collect_column_conditions(sub, sub_out)
            if sub_out:
                or_parts.append("(" + " AND ".join(sub_out) + ")")
        if or_parts:
            out.append("(" + " OR ".join(or_parts) + ")")
    else:
        for k, v in node.items():
            if k in _COLUMN_KEYS and v is not None:
                esc = str(v).replace("'", "''")
                out.append(f"{k} = '{esc}'")


def _db():
    return lancedb.connect(str(config.LANCEDB_DIR))


def _table_name() -> str:
    return config.COLLECTION_NAME


def _discover_dim() -> int:
    """Vector dimension, derived from the embedding function (never re-embed)."""
    return len(_get_ef()(["__dim_probe__"])[0])


def _open_table():
    """Get-or-create the LanceDB table. Schema locks the vector dimension."""
    db = _db()
    name = _table_name()
    if name in db.table_names():
        tbl = db.open_table(name)
        # Migration: older tables lack the email_date scalar column. Add it as a
        # nullable column (filled with nulls) so date-bounded queries work
        # without re-embedding the whole corpus. Cheap O(n) single pass.
        if "email_date" not in tbl.schema.names:
            try:
                n = tbl.count_rows()
                tbl.add_columns({"email_date": pa.nulls(n, pa.string())})
            except Exception:
                pass
        return tbl
    dim = _discover_dim()
    schema = pa.schema(
        [
            ("id", pa.string()),
            ("vector", pa.list_(pa.float32(), dim)),
            ("document", pa.string()),
            # metadata is stored as a JSON string so arbitrary dicts round-trip
            ("metadata", pa.string()),
            # Real scalar column for date-bounded retrieval (SQL-filtered).
            ("email_date", pa.string()),
        ]
    )
    try:
        return db.create_table(name, schema=schema)
    except Exception:
        # Another coroutine (warmup vs auto-sync) may have created the table
        # concurrently. Fall back to opening the existing one instead of crashing
        # with "Table '...' already exists".
        if name in db.table_names():
            return db.open_table(name)
        raise


def _chroma_where_to_lance(where):
    """Best-effort translation of Chroma's ``where`` dict into a LanceDB SQL
    predicate. Returns ``None`` when no filter should be applied (or the filter
    is something we don't understand -- we never want to crash a search)."""
    if not where:
        return None

    def esc(v):
        return "'" + str(v).replace("'", "''") + "'"

    def convert(node):
        if not isinstance(node, dict):
            return None
        # operators
        if "$and" in node:
            parts = [convert(s) for s in node["$and"]]
            parts = [p for p in parts if p]
            return "(" + " AND ".join(parts) + ")" if parts else None
        if "$or" in node:
            parts = [convert(s) for s in node["$or"]]
            parts = [p for p in parts if p]
            return "(" + " OR ".join(parts) + ")" if parts else None
        # single key/value (equality or $in)
        for key, val in node.items():
            if isinstance(val, dict) and "$in" in val:
                lst = ", ".join(esc(x) for x in val["$in"])
                return f"{key} IN ({lst})"
            return f"{key} = {esc(val)}"
        return None

    try:
        return convert(where)
    except Exception:
        return None


def _where_match(meta, where):
    """Chroma-style ``where`` match against a parsed metadata dict (Python-side,
    since LanceDB stores metadata as a JSON string column — no SQL column).

    Keys that map to real scalar columns (see ``_COLUMN_KEYS``) are skipped here
    because they are already handled by the SQL-level ``_column_predicate``.
    """
    if not where or not meta:
        return True
    if "$and" in where:
        return all(_where_match(meta, s) for s in where["$and"])
    if "$or" in where:
        return any(_where_match(meta, s) for s in where["$or"])
    for key, val in where.items():
        if key in _COLUMN_KEYS:
            continue
        if isinstance(val, dict) and "$in" in val:
            if meta.get(key) not in val["$in"]:
                return False
        else:
            if meta.get(key) != val:
                return False
    return True


def _empty_result() -> dict:
    """Empty Chroma-shaped response; normalize_retrieve() treats it as no hits."""
    return {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }


class LanceDBCollection:
    def add(self, ids, documents, metadatas):
        vectors = _get_ef()(list(documents))
        tbl = _open_table()
        # The production table (440k rows) was created WITHOUT the `email_date`
        # scalar column, and LanceDB's add_columns migration fails silently on
        # it -- so we must NOT write that field, or `add` raises
        # "field 'email_date' does not exist in table schema". Date-bounded
        # retrieval already works via the `email_date` key INSIDE the JSON
        # `metadata` column (Python-side `_where_match`), so dropping the scalar
        # column is lossless. Only include it when the table actually has it.
        has_ed = "email_date" in tbl.schema.names
        rows = [
            {
                "id": str(i),
                "vector": [float(x) for x in v],
                "document": doc,
                "metadata": json.dumps(meta or {}, ensure_ascii=False),
                **({"email_date": (meta or {}).get("email_date")} if has_ed else {}),
            }
            for i, v, doc, meta in zip(ids, vectors, documents, metadatas)
        ]
        tbl.add(rows)

    def upsert(self, ids, documents, metadatas):
        vectors = _get_ef()(list(documents))
        tbl = _open_table()
        has_ed = "email_date" in tbl.schema.names
        rows = [
            {
                "id": str(i),
                "vector": [float(x) for x in v],
                "document": doc,
                "metadata": json.dumps(meta or {}, ensure_ascii=False),
                **({"email_date": (meta or {}).get("email_date")} if has_ed else {}),
            }
            for i, v, doc, meta in zip(ids, vectors, documents, metadatas)
        ]
        # lancedb 0.34.0 has no Table.upsert(); emulate it with delete-existing
        # + add (both supported natively and verified to persist). This keeps
        # re-runs idempotent: any stale chunks for these ids are removed first,
        # then the fresh chunks are inserted.
        if ids:
            id_list = ", ".join("'" + str(i).replace("'", "''") + "'" for i in ids)
            tbl.delete(where=f"id IN ({id_list})")
        tbl.add(rows)

    def get(self, ids=None, include=None):
        # include is ignored for Chroma-compat; we return what ingest.py reads.
        tbl = _open_table()
        if ids:
            ds = tbl.to_lance()
            scanner = ds.scanner(columns=["id"], filter=pc.is_in(pc.field("id"), pa.array(list(ids))))
            found = scanner.to_table()["id"].to_pylist()
        else:
            found = tbl.to_lance().scanner(columns=["id"]).to_table()["id"].to_pylist()
        return {
            "ids": found,
            "documents": None,
            "metadatas": None,
            "embeddings": None,
        }

    def query(self, query_texts, n_results=5, where=None):
        q = _get_ef()(list(query_texts))[0]
        tbl = _open_table()
        col_pred = _column_predicate(where)
        if where or col_pred:
            # A real scalar-column filter (e.g. email_date) is applied at the
            # SQL level so LanceDB only scans the matching rows. Remaining
            # metadata-only keys (scope/user_id) are matched in Python over a
            # larger candidate pool (Chroma-compatible behaviour). Without any
            # filter we go straight to a precise top-n search.
            candidate_n = max(n_results * 20, 100)
            search = (
                tbl.search(q, vector_column_name="vector")
                .metric("cosine")
                .limit(candidate_n)
                .select(["id", "document", "metadata"])
            )
            if col_pred:
                search = search.where(col_pred)
            points = search.to_list()
            points = [
                p
                for p in points
                if _where_match(
                    json.loads(p["metadata"]) if p.get("metadata") else {}, where
                )
            ][:n_results]
        else:
            points = (
                tbl.search(q, vector_column_name="vector")
                .metric("cosine")
                .limit(n_results)
                .select(["id", "document", "metadata"])
                .to_list()
            )
        ids = [[r["id"] for r in points]]
        documents = [[r.get("document") for r in points]]
        metadatas = [
            [json.loads(r["metadata"]) if r.get("metadata") else {} for r in points]
        ]
        # LanceDB cosine distance == Chroma cosine distance (1 - similarity).
        distances = [[float(r["_distance"]) for r in points]]
        return {
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas,
            "distances": distances,
        }

    def _keyword_candidates(self, query, where, cap, extra_sql=None):
        """Lexical (keyword) recall: chunks whose text contains the query
        tokens, ranked by term-frequency so the chunk most *about* the topic
        wins.

        This compensates for the embedding model's weakness on rare / short
        tokens (e.g. a ticker like "CATL"). A bare "CATL" query returns little
        useful from pure vector search, but the chunks genuinely *about* CATL
        mention it many times and rise to the top here.

        Returns a list of {id, document, metadata, tf}, tf-sorted desc, capped.
        No new dependency: it pushes a Lance SQL ``LOWER(document) LIKE`` scan,
        which is fast on the local disk-backed store. ``extra_sql`` (e.g. a
        date-bounded predicate) is AND-ed in so short queries never scan the
        whole table.
        """
        terms = [
            t.lower()
            for t in re.findall(r"\w+", query or "", re.UNICODE)
            if len(t) >= 3 and not t.isdigit()
        ]
        if not terms:
            return []
        like_sql = " OR ".join(f"LOWER(document) LIKE '%{t}%'" for t in terms)
        sql = like_sql
        if extra_sql:
            sql = f"({like_sql}) AND ({extra_sql})"
        tbl = _open_table()
        tb = (
            tbl.to_lance()
            .scanner(columns=["id", "document", "metadata"], filter=sql)
            .to_table()
        )
        out = []
        for r in tb.to_pylist():
            doc_l = (r.get("document") or "").lower()
            tf = sum(doc_l.count(t) for t in terms)
            if tf <= 0:
                continue
            meta = json.loads(r["metadata"]) if r.get("metadata") else {}
            out.append(
                {
                    "id": r["id"],
                    "document": r.get("document"),
                    "metadata": meta,
                    "tf": tf,
                }
            )
        out.sort(key=lambda x: -x["tf"])
        return out[:cap]

    def _folder_ids(self, filter_paths: list[str]) -> pa.Array:
        """IDs of every row whose ``source`` lives under one of ``filter_paths``.

        Built from a (id, metadata)-only scan (no vector payload) and cached per
        sorted filter tuple. Returns an empty array when nothing matches.
        """
        key = tuple(sorted(filter_paths))
        cached = _FOLDER_IDS_CACHE.get(key)
        if cached is not None:
            return cached
        tbl = _open_table()
        arrow = tbl.to_lance().scanner(columns=["id", "metadata"]).to_table()
        ids = arrow.column("id").to_pylist()
        metas = arrow.column("metadata").to_pylist()
        folder_ids = [
            ids[i]
            for i in range(len(ids))
            if source_matches(json.loads(metas[i]).get("source", "") if metas[i] else "", filter_paths)
        ]
        arr = pa.array(folder_ids)
        _FOLDER_IDS_CACHE[key] = arr
        return arr

    @staticmethod
    def _search_subset(q, sub, n_results: int, where=None) -> dict:
        """Exact cosine top-N over a small Arrow subset (folder-scoped search),
        WITH per-file coverage.

        The subset is at most a few thousand rows, so a plain numpy scan is
        fast (<10ms) and sidesteps LanceDB's ANN API for filtered tables.
        A folder usually holds several files; a plain global top-N is dominated
        by the biggest file (e.g. a 5k-chunk news feed) and can starve every
        other file in the folder — the model then "reads" only one file. So we
        first take top-k per distinct source, then merge and keep the best
        ``n_results`` overall. With 3 files and n=6 that means 2 chunks per
        file, so a "summarise this folder" answer covers every file.
        """
        import numpy as np
        ids = sub.column("id").to_pylist()
        vecs = sub.column("vector").to_pylist()
        docs = sub.column("document").to_pylist()
        metas = sub.column("metadata").to_pylist()
        if not vecs:
            return _empty_result()
        qn = np.asarray(q, dtype=np.float32)
        qn = qn / (np.linalg.norm(qn) + 1e-9)
        vn = np.asarray(vecs, dtype=np.float32)
        vn = vn / (np.linalg.norm(vn, axis=1, keepdims=True) + 1e-9)
        sims = vn @ qn
        # Group row indices by source file.
        by_src: dict[str, list[int]] = {}
        for idx in range(len(ids)):
            meta = json.loads(metas[idx]) if metas[idx] else {}
            by_src.setdefault(meta.get("source", ""), []).append(idx)
        n_files = len(by_src)
        per_file = max(2, n_results // max(1, n_files))
        cand: list[int] = []
        for _src, idxs in by_src.items():
            order = np.argsort(-sims[idxs])[:per_file]
            cand.extend(int(i) for i in order)
        # Dedupe, keep the best n_results overall, then apply the where filter.
        cand = sorted(set(cand), key=lambda i: -float(sims[i]))[:n_results]
        ranked = []
        for idx in cand:
            meta = json.loads(metas[idx]) if metas[idx] else {}
            if where and not _where_match(meta, where):
                continue
            ranked.append({
                "id": ids[idx],
                "document": docs[idx],
                "metadata": meta,
                "sim": float(sims[idx]),
            })
        return {
            "ids": [[r["id"] for r in ranked]],
            "documents": [[r["document"] for r in ranked]],
            "metadatas": [[r["metadata"] for r in ranked]],
            "distances": [[round(1.0 - r["sim"], 6) for r in ranked]],
        }

    def hybrid_search(self, query_texts, n_results=5, where=None, alpha=0.6, use_keyword=True,
                      filter_paths=None):
        """Vector search blended with keyword recall.

        When ``filter_paths`` is given (the @Ask-about-folder/file picker), the
        vector scan itself is RESTRICTED to rows whose source lives under those
        paths — a global top-N scan + post-filter can never surface chunks from
        a small folder buried under hundreds of thousands of unrelated vectors.
        The folder's row IDs are built once per filter (cached) and the (small)
        subset is searched with an exact numpy cosine top-N.
        """
        q = _get_ef()(list(query_texts))[0]
        tbl = _open_table()

        # Folder/file-scoped search (@Ask about folder|file): restrict the ANN
        # scan to the folder's row subset instead of searching the whole table.
        if filter_paths:
            folder_ids = self._folder_ids(filter_paths)
            if len(folder_ids) == 0:
                return _empty_result()
            sub = (
                tbl.to_lance()
                .scanner(
                    filter=pc.is_in(pc.field("id"), folder_ids),
                    columns=["id", "vector", "document", "metadata"],
                )
                .to_table()
            )
            return self._search_subset(q, sub, n_results, where=where)

        cap = max(n_results * 3, 20)
        col_pred = _column_predicate(where)
        search = (
            tbl.search(q, vector_column_name="vector")
            .metric("cosine")
            .limit(cap)
            .select(["id", "document", "metadata"])
        )
        if col_pred:
            search = search.where(col_pred)
        vec_points = search.to_list()
        kw = (
            self._keyword_candidates(query_texts[0], where, cap=cap, extra_sql=col_pred)
            if use_keyword
            else []
        )

        combined = {}
        sims = []
        for r in vec_points:
            meta = json.loads(r["metadata"]) if r.get("metadata") else {}
            sim = 1.0 - float(r["_distance"])
            sims.append(sim)
            combined[r["id"]] = {
                "id": r["id"],
                "document": r.get("document"),
                "metadata": meta,
                "sim": sim,
                "kw": 0.0,
            }
        baseline = sorted(sims)[len(sims) // 2] if sims else 0.5
        max_tf = max((k["tf"] for k in kw), default=1) or 1
        for k in kw:
            if where and not _where_match(k["metadata"], where):
                continue
            kw_norm = k["tf"] / max_tf
            cur = combined.get(k["id"])
            if cur is None:
                combined[k["id"]] = {
                    "id": k["id"],
                    "document": k["document"],
                    "metadata": k["metadata"],
                    "sim": baseline,
                    "kw": kw_norm,
                }
            else:
                cur["kw"] = max(cur["kw"], kw_norm)

        ranked = sorted(
            combined.values(), key=lambda x: -(x["sim"] + alpha * x["kw"])
        )[:n_results]
        ids = [[r["id"] for r in ranked]]
        documents = [[r["document"] for r in ranked]]
        metadatas = [[r["metadata"] for r in ranked]]
        distances = [[round(1.0 - r["sim"], 6) for r in ranked]]
        return {
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas,
            "distances": distances,
        }

    def count(self):
        return _open_table().count_rows()

    def delete(self, ids=None, where=None):
        tbl = _open_table()
        if ids:
            id_list = ", ".join("'" + str(i).replace("'", "''") + "'" for i in ids)
            tbl.delete(where=f"id IN ({id_list})")
        elif where:
            pred = _chroma_where_to_lance(where)
            if pred:
                tbl.delete(where=pred)

    def get_metadatas(self, ids):
        """Return a list (parallel to ``ids``) of parsed metadata dicts.

        Used by ingest.py to read the stored ``content_hash`` / ``chunk_count``
        of a document without a full table scan. Missing ids yield ``None``.
        """
        if not ids:
            return []
        tbl = _open_table()
        ds = tbl.to_lance()
        scanner = ds.scanner(
            columns=["id", "metadata"],
            filter=pc.is_in(pc.field("id"), pa.array(list(ids))),
        )
        rows = scanner.to_table().to_pylist()
        by_id = {}
        for r in rows:
            meta = r.get("metadata")
            by_id[r["id"]] = json.loads(meta) if meta else None
        return [by_id.get(i) for i in ids]

    def delete_by_doc_id(self, doc_id):
        """Delete every chunk row whose id starts with ``f"{doc_id}_"``.

        Used when re-embedding a file whose stored ``chunk_count`` is unknown
        (e.g. a row written before chunk_count tracking existed). The full id
        scan is only triggered in that rare case, so normal ingestion is
        unaffected.
        """
        prefix = f"{doc_id}_"
        tbl = _open_table()
        all_ids = (
            tbl.to_lance().scanner(columns=["id"]).to_table()["id"].to_pylist()
        )
        target = [i for i in all_ids if isinstance(i, str) and i.startswith(prefix)]
        if target:
            self.delete(ids=target)
