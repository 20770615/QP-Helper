"""Shared path-scoping matcher for @Ask-about-folder / @Ask-about-file.

Kept as a leaf module (no imports from the rest of ``rag``) so both the store
(``rag/store_lancedb.py``) and the harness (``rag_service/harness.py``) can use
it without a circular import.

Why segments instead of prefixes:
    The same shared folder is often addressable as both a UNC path
    (``\\\\server\\share\\...``) and a mapped drive letter (``G:\\...``).
    We compare normalized, lowercased path *segments* rather than by exact
    prefix, and a root-stripped *suffix run* is accepted too — so a folder
    picked from a dialog that starts at ``G:/QAML-qam/Investment Team/``
    (drive form) still scopes correctly against vectors ingested under
    ``\\\\WWFS01\\Group\\QAML-qam\\Investment Team\\...`` (UNC form), whose
    leading root segments differ in both name and count.
"""

from __future__ import annotations


def _split_segs(p: str) -> list[str]:
    """Normalized forward-slash path split into non-empty lowercased segments."""
    p = (p or "").replace("\\", "/").strip("/")
    return [s for s in p.lower().split("/") if s]


def _segs_contained(f_segs: list[str], s_segs: list[str]) -> bool:
    """True if ``f_segs`` appears as a consecutive run inside ``s_segs``."""
    L = len(f_segs)
    if L == 0 or L > len(s_segs):
        return False
    if L == len(s_segs):
        return f_segs == s_segs
    return any(s_segs[i:i + L] == f_segs for i in range(len(s_segs) - L + 1))


def _folder_run_contained(f_segs: list[str], s_segs: list[str]) -> bool:
    """True if the folder filter's path matches the source path.

    The full filter path, or any *suffix* of it that is at least two segments
    (i.e. the root stripped — bridges drive-letter vs UNC roots), must appear
    as a consecutive run inside the source's segments. A single-segment filter
    is only honored as the whole filter (so a generic word like "data" cannot
    over-match on unrelated paths, while a short folder name such as
    "News collection tools" still works).
    """
    L = len(f_segs)
    if L == 0 or not s_segs:
        return False
    # Only try suffixes that can actually fit inside the source path.
    first = max(0, L - len(s_segs))
    for start in range(first, L):
        win = f_segs[start:]
        if len(win) < 2 and start != 0:
            continue  # root-stripped single segment is too generic
        if _segs_contained(win, s_segs):
            return True
    return False


def source_matches(src: str, filters: list[str]) -> bool:
    """Whether a chunk's ``source`` path lives under one of ``filters``.

    A filter whose last segment contains a ``.`` is treated as a *file*: it
    matches when the source ends in that file (same basename, any parent). A
    directory filter matches any source living inside it (with the
    UNC/drive-letter root tolerance described in the module docstring).
    """
    s_segs = _split_segs(src)
    if not s_segs:
        return False
    for f in filters:
        f_segs = _split_segs(f)
        if not f_segs:
            continue
        # File filter: last segment has an extension -> require exact basename.
        if "." in f_segs[-1]:
            if s_segs[-1] == f_segs[-1] or _segs_contained(f_segs, s_segs):
                return True
            continue
        # Folder filter: full path or root-stripped suffix run.
        if _folder_run_contained(f_segs, s_segs):
            return True
    return False
