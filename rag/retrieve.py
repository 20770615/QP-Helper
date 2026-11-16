"""Retrieval: filtered similarity search over the Chroma collection.

Phase 1 only retrieves relevant chunks (no LLM call). A clearly marked
generate_answer() extension point is provided for Phase 3's LLM harness but is
NOT called anywhere in this phase.
"""

import re

from rag import config
from rag.store import get_collection


def retrieve(query: str, scope=None, user_id=None, n: int = 5, date_filter: str | None = None,
             filter_paths: list[str] | None = None) -> dict:
    """Run a similarity search in cosine space.

    Args:
        query:        the natural-language query.
        scope:        optional metadata filter (Phase 2 will filter by real scope).
        user_id:      optional metadata filter (Phase 2 will filter by real user).
        n:            number of results.
        date_filter:  optional ``YYYY-MM-DD`` string. When set, retrieval is
                      bounded to chunks whose ``email_date`` equals this date
                      (a real LanceDB scalar column, SQL-filtered — so the scan
                      only touches that day's rows instead of the whole corpus).
                      Used for "昨天/今天/某天 的邮件" style questions.
        filter_paths: optional folder/file paths from the frontend's
                      @Ask-about-folder / @Ask-about-file picker. When set, the
                      vector search is RESTRICTED to chunks whose source lives
                      under one of these paths (the store bounds the ANN scan
                      itself — a global top-N scan + post-filter always misses
                      small folders buried under hundreds of thousands of
                      unrelated vectors).

    Returns:
        Chroma query response dict (ids / documents / metadatas / distances).
    """
    collection = get_collection()
    # Build the metadata filter. Chroma (>=0.4) rejects a flat dict with more
    # than one key — it requires an explicit operator. A single condition stays
    # as a flat equality filter; multiple conditions are wrapped in $and.
    conditions = []
    if scope is not None:
        conditions.append({"scope": scope})
    if user_id is not None:
        conditions.append({"user_id": user_id})
    if date_filter:
        # email_date is a real scalar column, so it can be SQL-filtered at query
        # time (see _column_predicate in store_lancedb.py).
        conditions.append({"email_date": date_filter})
    if not conditions:
        where = None
    elif len(conditions) == 1:
        where = conditions[0]
    else:
        where = {"$and": conditions}

    # Only run the expensive full-table keyword scan for short / lexical
    # queries (a ticker like "CATL", a single term). Natural-language questions
    # are handled well by vector search alone, and skipping the scan shaves
    # ~1.5-2.6s off every retrieval call inside an agent turn.
    tokens = re.findall(r"\w+", query or "", re.UNICODE)
    meaningful = [t for t in tokens if len(t) >= 3 and not t.isdigit()]
    use_keyword = len(meaningful) <= 3 and not filter_paths

    return collection.hybrid_search(
        query_texts=[query],
        n_results=n,
        where=where,
        use_keyword=use_keyword,
        filter_paths=filter_paths,
    )


def format_results(res: dict, max_chars: int = 500) -> str:
    """Render a query response as readable console text, including metadata."""
    if not res or not res.get("ids") or not res["ids"][0]:
        return "(no results)"

    ids = res["ids"][0]
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = (res.get("distances") or [[]])[0]

    lines: list[str] = []
    for idx, (cid, doc, meta) in enumerate(zip(ids, docs, metas)):
        dist = dists[idx] if idx < len(dists) else None
        sim = (1.0 - dist) if dist is not None else None
        header = f"--- Result {idx + 1} | id={cid}"
        if sim is not None:
            header += f" | similarity={sim:.4f}"
        lines.append(header)
        lines.append(f"  source     : {meta.get('source')}")
        lines.append(f"  user_id    : {meta.get('user_id')}")
        lines.append(f"  scope      : {meta.get('scope')}")
        lines.append(
            f"  doc_id     : {meta.get('doc_id')}  chunk_index={meta.get('chunk_index')}"
        )
        lines.append(f"  created_at : {meta.get('created_at')}")
        snippet = doc if len(doc) <= max_chars else doc[:max_chars] + " …"
        lines.append("  content:")
        for ln in snippet.splitlines():
            lines.append("    " + ln)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extension point for Phase 3 (LLM harness). Intentionally NOT called in Phase 1.
# ---------------------------------------------------------------------------
def generate_answer(query: str, context: str) -> str:
    """Placeholder for the Phase 3 LLM call (team's own, vendor-agnostic API).

    Phase 1 must not perform any external LLM call, so this raises if invoked.
    When Phase 3 lands, implement it against the team's own LLM API and pass the
    retrieved `context` (e.g. format_results output) to the model.
    """
    raise NotImplementedError(
        "generate_answer() is an extension point for Phase 3 (LLM harness) "
        "and is intentionally not called in Phase 1."
    )
