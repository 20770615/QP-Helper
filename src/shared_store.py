"""Shared-drive data store for QP Email Helper (ui_reorg_prompt §6).

All team-shared data lives on the shared drive root (default G: drive).
The knowledge base now spans the WHOLE shared root (user chose "show all"):
every sub-folder is a KB category; tags/colors live at the root.

    <shared_root>/            # = knowledge base root
        <folder>/<file>...    # each sub-folder is a KB category
        .tags.jsonl           # {"file": "<rel path>", "tags": [...]}
        tag_colors.json       # {"tag": "#color"}
        prompts/prompts.json  # [{"id","title","content","created_by","updated_at"}]
        ucits_fund_holdings.csv
        agent_config.json     # {"system_prompt": "...", ...}
        upload_log.jsonl      # {"user","time","filename","folder"}

Plain file I/O only — nothing here touches Chroma or the LLM. Concurrency-safe:
append writes use append mode; full rewrites write a temp file then rename.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile  # noqa: F401 — present in original bytecode
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config_manager import APP_ROOT, get_actor, get_shared_root
from metadata_cache import remove as metadata_remove

_TAG_PALETTE = [
    "#4F8EF7",
    "#F7A24F",
    "#22C55E",
    "#A855F7",
    "#EF4444",
    "#14B8A6",
    "#EAB308",
    "#EC4899",
]
_DEFAULT_TAG_COLORS: dict[str, str] = {
    "信息科技": "#4F8EF7",
    "宏观分析": "#F7A24F",
    "笼统日报": "#22C55E",
    "日常消费": "#A855F7",
}

KB_HIDDEN_FOLDERS = {"Email reports"}
KB_VISIBLE_FOLDERS: set[str] | None = None  # None => show all (after hidden/skip filters)
KB_SKIP_DIRS = {
    ".venv",
    ".idea",
    "dist",
    ".git",
    "node_modules",
    "venv",
    ".vscode",
    "build",
    "__pycache__",
}

_FILE_HASH_CACHE: dict | None = None
_FILE_HASH_CACHE_PATH: Path | None = None

DEFAULT_SYSTEM_PROMPT = (
    "You are an elite Equity Research Analyst at Quam Asset Management. Your primary mandate "
    "is to support the Lead Portfolio Manager by recognizing investment potential from daily "
    "news dynamics and the team's internal research, and by proposing weight adjustments or "
    "new-stock-selection ideas on a long-only basis. You generate high-conviction, investable "
    "ideas and strict rationales for every suggestion you make.\n"
    "\n"
    "IMPORTANT: You are a research assistant that answers the user's questions directly. Do NOT "
    "force a fixed report format. Simply answer what is asked, but always reason through the "
    "analyst lens below (UCITS constraints, three-pillar philosophy, citations, self-critique). "
    "Only use the report structure in the final section when the user explicitly asks for a "
    "report or portfolio review.\n"
    "\n"
    "# KNOWLEDGE BOUNDARY (HARD CONSTRAINT)\n"
    "- You have NO web access and NO external data/market APIs. ALL research, prices, "
    "fundamentals, valuations, and catalysts MUST come ONLY from the connected knowledge base "
    "(G: documents): ingested broker-research emails (today_emails.json, last 24h), the "
    '"UCITS Fund" positions file, meeting transcripts, and other team documents retrievable '
    "via retrieve_context.\n"
    "- If the data you need is NOT in the KB, you MUST state "
    '"知识库中未找到相关信息 / not found in the knowledge base" and MUST NOT fabricate numbers, '
    "prices, or facts.\n"
    "- Every factual claim MUST be cited with its retrieved chunk id [ #n ] and source file.\n"
    "- Do NOT echo the raw retrieved text in the final answer. The UI already shows every "
    "retrieved chunk BELOW your answer as a clickable source card (original text + a link to "
    "open the file), so repeating that text in your answer is pure duplication. Write the "
    "answer 100% in your OWN words — never copy sentences, clauses, tables, data rows, or "
    "bullet lists from the knowledge base, not even short ones. Paraphrase everything and "
    "point to evidence with [ #n ] citations. Copying is redundant (the user already sees it "
    "in the card) and will be stripped by the system, so it is wasted effort. If you must "
    "mention a specific figure, restate it in your own phrasing and cite the source card.\n"
    "\n"
    "# CROSS-LINGUAL RETRIEVAL\n"
    "- The KB is predominantly English broker research. If the user queries in Chinese, FIRST "
    "rewrite the query into one or more precise English retrieval queries (entity + key terms) "
    "before calling retrieve_context, to maximize recall against the English corpus. Never "
    "answer from parametric memory.\n"
    "\n"
    "# CONTEXT WHEN RELEVANT\n"
    "- When the task involves current news or positions, scan all emails ingested in the last "
    '24h and load the "UCITS Fund" file for current positions. Target Date = the date of the '
    "latest ingested email batch (typically today).\n"
    "\n"
    "# STRICT PORTFOLIO WEIGHT CONSTRAINTS (UCITS, Greater China Mandate)\n"
    "- Regional Scope: the portfolio MUST ONLY invest in the Greater China region (Mainland "
    "China, Hong Kong, Macau, Taiwan). Any selected stock outside this region MUST have "
    "allocation 0%.\n"
    "- Single Position Limit: no single long position may exceed 10% of NAV.\n"
    "- 5/40 Rule: the aggregate value of all positions that individually exceed 5% of NAV must "
    "not exceed 40% of total NAV.\n"
    "- China A-Share Limit: maximum market exposure strictly less than 30%.\n"
    "- Taiwan Market Limit: maximum market exposure strictly less than 40%.\n"
    "- Cash Limit: cash allocation strictly less than 20%.\n"
    "- Position Count: the UCITS portfolio must hold between 23 and 27 stocks at all times.\n"
    "\n"
    "# INVESTMENT PHILOSOPHY (three pillars — every rationale must adhere)\n"
    "- Business Quality: economic moats, ROIC, balance sheet strength, corporate governance.\n"
    "- Valuation: absolute/relative multiples, FCF yield, margin of safety.\n"
    "- Operating Momentum: top-line growth, margin expansion/contraction, macro "
    "tailwinds/headwinds directly impacting specific earnings.\n"
    "\n"
    "# RESEARCH DISCIPLINE\n"
    "- Company-First Focus: 90%+ of analytical output dedicated to individual stock research "
    "and fundamental analysis.\n"
    "- Strict Macro Condition: general macro commentary is PROHIBITED. Discuss "
    "macro/regulatory environment ONLY if tied explicitly to a specific company's operating "
    "momentum or earnings power.\n"
    "\n"
    "# OPTIONAL REPORT STRUCTURE (only when the user explicitly asks for a report / portfolio "
    "review)\n"
    "### File Name: Global Equity Opportunities Report - [Target Date YYYYMMDD] - UCITS "
    "Portfolio Allocation\n"
    "### Part 1: Big Event Summary (event - source - importance, past 24h)\n"
    "### Part 2: Idea Generation Summary (3-4 sentences; overarching themes)\n"
    "### Part 3: Long-Only Investable Ideas - for EVERY selected stock, exact structure "
    "(do not group):\n"
    "[Company Name / Ticker]\n"
    "- Current Price: [from KB only; if absent state \"知识库中未找到\"] - Target Date\n"
    "- Investment Rationale:\n"
    "  * Business Quality: [moat, ROIC, balance sheet based on KB]\n"
    "  * Valuation: [multiples, FCF yield, margin of safety based on KB]\n"
    "  * Operating Momentum: [growth, margin trajectory, catalysts based on KB]\n"
    "- Entry Point & Rationale: [price] - why optimal.\n"
    "- Target Exit Point & Rationale: [price] - exit mechanism.\n"
    "- UCITS Allocation (% Weight): [ % ] - justify size; confirm compliance with Greater "
    "China + UCITS limits; 0% if out of region.\n"
    "### Part 4: Proposed Portfolio Allocation Table (Markdown)\n"
    "| Company / Ticker | Current Price | Target Entry | Target Exit | Proposed Weight (%) | "
    "Portfolio Constraint Check (UCITS) |\n"
    "CRITICAL: verify final proposed weights AND the 23-27 stock count mathematically comply "
    "before finalizing.\n"
    "\n"
    "# SELF-CRITIQUE (before finalizing any allocation suggestion)\n"
    "- Do the final weights AND the 23-27 stock count mathematically comply with ALL UCITS "
    "limits?\n"
    "- Does every factual claim carry a [ #n ] citation + source file?\n"
    "- Is anything fabricated or sourced from outside the KB?\n"
    '- Is missing data explicitly flagged as "知识库中未找到"?\n'
    "\n"
    "# RESPONSE LANGUAGE (CRITICAL — MUST FOLLOW)\n"
    "- You MUST reply in the EXACT same language the user wrote their latest message in.\n"
    "- User wrote in English -> answer in English. User wrote in Chinese -> answer in Chinese.\n"
    "- Do NOT translate your reply into Chinese just because this prompt, the KB, or the "
    "examples contain Chinese. Reply language is driven ONLY by the user's input language.\n"
    "- (The Chinese->English query rewrite rule above applies ONLY to searching the KB, NEVER "
    "to your final answer language.)"
)


def shared_root() -> Path:
    return Path(get_shared_root())


def kb_root() -> Path:
    """KB root is the shared root (show-all mode)."""
    return shared_root()


def prompts_file() -> Path:
    return shared_root() / "prompts" / "prompts.json"


def upload_log_file() -> Path:
    return shared_root() / "upload_log.jsonl"


def agent_config_file() -> Path:
    return APP_ROOT / "_internal" / "agent_config.json"


def holdings_default_file() -> Path:
    return shared_root() / "ucits_fund_holdings.csv"


def tags_file() -> Path:
    return kb_root() / ".tags.jsonl"


def tag_colors_file() -> Path:
    return kb_root() / "tag_colors.json"


def ensure_kb() -> None:
    kb_root().mkdir(parents=True, exist_ok=True)


def list_folders() -> list[str]:
    """Return immediate sub-folder names of the knowledge base root."""
    root = kb_root()
    if not root.exists():
        return []
    names = [
        p.name
        for p in root.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and p.name not in KB_HIDDEN_FOLDERS
        and p.name not in KB_SKIP_DIRS
    ]
    names = [n for n in names if n not in KB_HIDDEN_FOLDERS]
    if KB_VISIBLE_FOLDERS is not None:
        names = [n for n in names if n in KB_VISIBLE_FOLDERS]
    return sorted(names)


def list_subfolders(rel: str | None = None) -> list[str]:
    """Return immediate sub-folder NAMES directly under ``kb_root()/<rel>``

    (``rel=None`` -> the KB root). Names only — callers build the relative
    path as ``(rel + "/" + name)``. Hidden folders, the pure
    code/dependency dirs in ``KB_SKIP_DIRS`` and ``KB_HIDDEN_FOLDERS`` are
    pruned; the ``KB_VISIBLE_FOLDERS`` whitelist (if any) is applied only at
    the top level so drilling into a whitelisted folder still reveals its
    children.
    """
    if rel:
        parts = [p for p in rel.split("/") if p]
        base = kb_root().joinpath(*parts)
    else:
        base = kb_root()
    if not base.exists() or not base.is_dir():
        return []
    skip = KB_HIDDEN_FOLDERS | KB_SKIP_DIRS
    names = [
        p.name
        for p in base.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in skip
    ]
    if KB_VISIBLE_FOLDERS is not None and (rel is None or rel == ""):
        names = [n for n in names if n in KB_VISIBLE_FOLDERS]
    return sorted(names)


def _rel(path: Path) -> str:
    """Relative posix path of a file against the kb root."""
    try:
        return path.relative_to(kb_root()).as_posix()
    except ValueError:
        return path.name


def _collect_files(base: Path) -> list[Path]:
    """Recursively collect files, pruning hidden & hidden-KB folders.

    Pruning with os.walk (instead of rglob) means we never even enumerate
    inside ignored folders — important for performance on the shared drive.
    """
    if not base.exists():
        return []
    out: list[Path] = []
    base_str = str(base)
    for root, dirs, files in os.walk(base_str):
        skip = KB_HIDDEN_FOLDERS | KB_SKIP_DIRS
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
        for f in files:
            if f.startswith("."):
                continue
            if f.lower().endswith(".tmp"):
                continue
            out.append(Path(root) / f)
    out.sort(key=lambda p: p.name.lower())
    return out


def list_files(folder: str | None = None, with_hash: bool = False) -> list[dict[str, Any]]:
    """List files. folder=None -> all files recursively; else files in that folder.

    Returns list of {rel_path, abs_path, name, folder, tags}.

    When ``with_hash=True`` each entry also carries ``content_hash`` (SHA-256 of
    the file's bytes), used by the DB browser to hide files the user removed
    from the vector DB (excluded by content_hash). Folder-scoped listings hash
    live (cheap, a folder is small); the recursive "all files" listing reads the
    hash cache only (no live hashing) so it can't freeze the UI.
    """
    root = kb_root()
    if folder:
        base = root / folder
        files = [
            p
            for p in base.glob("*")
            if p.is_file() and not p.name.startswith(".") and not p.name.lower().endswith(".tmp")
        ]
        files.sort(key=lambda p: p.name.lower())
    else:
        files = _collect_files(root)

    tags = read_tags()
    result: list[dict[str, Any]] = []
    for p in files:
        rel = _rel(p)
        if folder:
            folder_val: str = folder
        elif "/" in rel:
            folder_val = rel.rsplit("/", 1)[0]
        else:
            folder_val = ""
        d: dict[str, Any] = {
            "rel_path": rel,
            "abs_path": str(p),
            "name": p.name,
            "folder": folder_val,
            "tags": tags.get(rel, []),
        }
        if with_hash:
            d["content_hash"] = _content_hash_cached(p, live=folder is not None)
        result.append(d)
    return result


def _file_hash_cache_path() -> Path:
    global _FILE_HASH_CACHE_PATH
    if _FILE_HASH_CACHE_PATH is None:
        _FILE_HASH_CACHE_PATH = APP_ROOT / "_internal" / "file_hashes.json"
    return _FILE_HASH_CACHE_PATH


def _load_file_hash_cache() -> dict:
    global _FILE_HASH_CACHE
    if _FILE_HASH_CACHE is not None:
        return _FILE_HASH_CACHE
    p = _file_hash_cache_path()
    _FILE_HASH_CACHE = {}
    if p.exists():
        try:
            _FILE_HASH_CACHE = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _FILE_HASH_CACHE = {}
    return _FILE_HASH_CACHE


def _save_file_hash_cache() -> None:
    if _FILE_HASH_CACHE is None:
        return
    p = _file_hash_cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_FILE_HASH_CACHE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        return


def _content_hash_cached(p: Path, live: bool = True) -> str:
    """SHA-256 of file bytes, cached by (normcase path, mtime_ns).

    ``live=False`` returns only a cached hash (never hashes) so the heavy
    recursive "all files" listing can't freeze the UI.
    """
    try:
        key = os.path.normcase(str(p))
        cache = _load_file_hash_cache()
        mtime = p.stat().st_mtime_ns
        entry = cache.get(key)
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == mtime:
            return entry[1]
        if not live:
            return ""
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(blk)
        value = h.hexdigest()
        cache[key] = [mtime, value]
        _save_file_hash_cache()
        return value
    except Exception:
        return ""


def add_folder(name: str) -> bool:
    """Create a new folder under the knowledge base. Returns True on success."""
    name = (name or "").strip()
    if not name:
        return False
    target = kb_root() / name
    target.mkdir(parents=True, exist_ok=True)
    return True


def delete_file(abs_path: str) -> None:
    """Delete a file from disk, tags and metadata cache."""
    p = Path(abs_path)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass
    rel = _rel(p)
    _remove_tag_entry(rel)
    try:
        metadata_remove(str(p))
    except Exception:
        pass


def copy_file_into_kb(src_path: str, folder: str) -> str | None:
    """Copy a local file into kb/<folder>. Returns the destination abs path."""
    src = Path(src_path)
    if not src.exists():
        return None
    ensure_kb()
    dest_dir = kb_root() / folder if folder else kb_root()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists() and dest.resolve() != src.resolve():
        stem, suffix = dest.stem, dest.suffix
        i = 1
        while (dest_dir / f"{stem} ({i}){suffix}").exists():
            i += 1
        dest = dest_dir / f"{stem} ({i}){suffix}"
    shutil.copy2(src, dest)
    return str(dest)


def delete_folder(rel: str) -> bool:
    """Delete a folder (and its contents) from the KB, plus its tag entries.

    Returns True if the folder existed and was removed. Raises on a
    permission/lock error so the caller can surface a friendly message rather
    than silently losing data.
    """
    parts = [p for p in (rel or "").split("/") if p]
    if not parts:
        return False
    target = kb_root().joinpath(*parts)
    if not target.exists() or not target.is_dir():
        return False
    shutil.rmtree(target)
    prefix = "/".join(parts) + "/"
    all_tags_map = read_tags()
    changed = False
    for k in list(all_tags_map.keys()):
        if k == prefix[:-1] or k.startswith(prefix):
            del all_tags_map[k]
            changed = True
    if changed:
        _save_tags_atomic(all_tags_map)
    return True


def move_file(rel: str, new_rel: str) -> str | None:
    """Move (rename) a file to a NEW relative path. This is a real move, NOT a
    copy. Updates the tag entry key. Returns the new rel_path, or None if the
    source is missing. Raises on a lock/permission error.
    """
    if not rel or not new_rel:
        return None
    src_parts = [p for p in rel.split("/") if p]
    new_parts = [p for p in new_rel.split("/") if p]
    if not src_parts or not new_parts:
        return None
    src = kb_root().joinpath(*src_parts)
    dest = kb_root().joinpath(*new_parts)
    if not src.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.resolve() != src.resolve():
        stem, suffix = dest.stem, dest.suffix
        i = 1
        while (dest.parent / f"{stem} ({i}){suffix}").exists():
            i += 1
        dest = dest.parent / f"{stem} ({i}){suffix}"
    shutil.move(str(src), str(dest))
    tags = read_tags()
    old_tags = tags.pop(rel, [])
    new_rel_out = _rel(dest)
    if old_tags:
        tags[new_rel_out] = old_tags
    _save_tags_atomic(tags)
    try:
        metadata_remove(str(src))
    except Exception:
        pass
    return new_rel_out


def copy_file_to(rel: str, dest_folder_rel: str) -> str | None:
    """Copy a KB file into another folder (paste). Returns the new rel_path,
    or None if the source is missing. Collision-safe naming.
    """
    if not rel:
        return None
    src_parts = [p for p in rel.split("/") if p]
    if not src_parts:
        return None
    src = kb_root().joinpath(*src_parts)
    if not src.exists():
        return None
    dest_parts = [p for p in (dest_folder_rel or "").split("/") if p]
    dest_dir = kb_root().joinpath(*dest_parts) if dest_parts else kb_root()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists() and dest.resolve() != src.resolve():
        stem, suffix = dest.stem, dest.suffix
        i = 1
        while (dest_dir / f"{stem} ({i}){suffix}").exists():
            i += 1
        dest = dest_dir / f"{stem} ({i}){suffix}"
    shutil.copy2(src, dest)
    return _rel(dest)


def read_tags() -> dict[str, list[str]]:
    """Return {rel_path: [tags]}. rel_path is posix-relative to kb root."""
    f = tags_file()
    out: dict[str, list[str]] = {}
    if not f.exists():
        return out
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rel = obj.get("file")
            tags = obj.get("tags", [])
            if rel:
                out[rel] = tags
    except Exception:
        return out
    return out


def write_tags(rel_path: str, tags: list[str]) -> None:
    """Set tags for a file (rewrites the whole .tags.jsonl atomically).

    New tags are assigned a palette color so ``all_tags()`` stays in sync.
    """
    for tag in tags:
        ensure_tag_color(tag)
    all_tags_map = read_tags()
    all_tags_map[rel_path] = tags
    _save_tags_atomic(all_tags_map)


def _remove_tag_entry(rel_path: str) -> None:
    all_tags_map = read_tags()
    if rel_path in all_tags_map:
        del all_tags_map[rel_path]
        _save_tags_atomic(all_tags_map)


def _save_tags_atomic(all_tags: dict[str, list[str]]) -> None:
    f = tags_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".jsonl.tmp")
    lines = [
        json.dumps({"file": k, "tags": v}, ensure_ascii=False) for k, v in all_tags.items()
    ]
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(f)


def read_tag_colors() -> dict[str, str]:
    f = tag_colors_file()
    if not f.exists():
        _save_tag_colors(_DEFAULT_TAG_COLORS)
        return dict(_DEFAULT_TAG_COLORS)
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return dict(_DEFAULT_TAG_COLORS)


def all_tags() -> list[str]:
    """All known tag names (from colors map)."""
    return sorted(read_tag_colors().keys())


def ensure_tag_color(tag: str) -> str:
    """Return the color for a tag, assigning one from the palette if new."""
    colors = read_tag_colors()
    if tag in colors:
        return colors[tag]
    existing = set(colors.values())
    for c in _TAG_PALETTE:
        if c not in existing:
            colors[tag] = c
            _save_tag_colors(colors)
            return c
    idx = len(colors) % len(_TAG_PALETTE)
    colors[tag] = _TAG_PALETTE[idx]
    _save_tag_colors(colors)
    return colors[tag]


def _save_tag_colors(colors: dict[str, str]) -> None:
    f = tag_colors_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(colors, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def rename_tag(old: str, new: str) -> None:
    """Rename ``old`` tag to ``new`` across the color map and all file tags."""
    old = old.strip()
    new = new.strip()
    if not old or not new or old == new:
        return
    colors = read_tag_colors()
    if old in colors:
        color = colors.pop(old)
        if new not in colors:
            colors[new] = color
        _save_tag_colors(colors)
    all_tags_map = read_tags()
    changed = False
    for rel, tags in list(all_tags_map.items()):
        if old in tags:
            all_tags_map[rel] = [new if tag == old else tag for tag in tags]
            changed = True
    if changed:
        _save_tags_atomic(all_tags_map)


def delete_tag(name: str) -> None:
    """Remove ``name`` tag from the color map and from every file's tag list."""
    name = name.strip()
    if not name:
        return
    colors = read_tag_colors()
    if name in colors:
        del colors[name]
        _save_tag_colors(colors)
    all_tags_map = read_tags()
    changed = False
    for rel, tags in list(all_tags_map.items()):
        if name in tags:
            tags.remove(name)
            changed = True
    if changed:
        _save_tags_atomic(all_tags_map)


def append_upload(filename: str, folder: str) -> None:
    """Append an upload record: {user, time, filename, folder}."""
    f = upload_log_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "user": get_actor(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "filename": filename,
        "folder": folder or "",
    }
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_uploads(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` upload records (newest first)."""
    f = upload_log_file()
    if not f.exists():
        return []
    lines: list[dict[str, Any]] = []
    try:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return lines[-limit:][::-1]


def load_prompts() -> list[dict[str, Any]]:
    f = prompts_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_prompts_atomic(prompts: list[dict[str, Any]]) -> None:
    f = prompts_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def add_prompt(title: str, content: str) -> dict[str, Any]:
    prompts = load_prompts()
    now = datetime.now().isoformat(timespec="seconds")
    item = {
        "id": f"p{int(time.time() * 1000)}",
        "title": title.strip(),
        "content": content,
        "created_by": get_actor(),
        "updated_at": now,
    }
    prompts.append(item)
    _save_prompts_atomic(prompts)
    return item


def update_prompt(prompt_id: str, title: str, content: str) -> None:
    prompts = load_prompts()
    for p in prompts:
        if p.get("id") == prompt_id:
            p["title"] = title.strip()
            p["content"] = content
            p["updated_at"] = datetime.now().isoformat(timespec="seconds")
            break
    _save_prompts_atomic(prompts)


def delete_prompt(prompt_id: str) -> None:
    prompts = [p for p in load_prompts() if p.get("id") != prompt_id]
    _save_prompts_atomic(prompts)


def load_agent_config() -> dict[str, Any]:
    f = agent_config_file()
    if not f.exists():
        return {"system_prompt": DEFAULT_SYSTEM_PROMPT, "philosophy": ""}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if "system_prompt" not in data:
            data["system_prompt"] = DEFAULT_SYSTEM_PROMPT
        return data
    except Exception:
        return {"system_prompt": DEFAULT_SYSTEM_PROMPT, "philosophy": ""}


def save_agent_config(system_prompt: str, philosophy: str = "") -> None:
    cfg = load_agent_config()
    cfg["system_prompt"] = system_prompt
    if philosophy:
        cfg["philosophy"] = philosophy
    _save_agent_config_atomic(cfg)


def _save_agent_config_atomic(cfg: dict[str, Any]) -> None:
    f = agent_config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
