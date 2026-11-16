"""Pluggable LLM harness for the RAG knowledge-base service.

This module is the *single* integration point for swapping the answer engine.
To use a different open-source framework later, implement a new subclass of
``BaseHarness`` and return it from ``get_harness()`` — nothing else in the
service or the app needs to change.

The default implementation, ``OpenAIAgentsHarness``, is built on the OpenAI
Agents SDK (the same engine behind Codex): it exposes RAG retrieval as a
``function_tool`` so the agent decides when to search, and answers are returned
with citations drawn from the retrieved chunks.

IMPORTANT — no hardcoded secrets:
    API key / base_url / model are injected from external configuration
    (the app's Settings page, environment variables, or ``POST /config``).
    This file never contains a key.

NOTE on the ``rag`` package contract:
    ``rag.retrieve.retrieve(...)`` returns the **raw Chroma response dict**
    (``{ids, documents, metadatas, distances}``), NOT a normalized list. The
    app-facing harness contract (and the UI) expect a list of
    ``{text, metadata, score}``. We normalize *here*, inside the harness, so
    the ``rag/`` package logic is left untouched.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


# ── Folder / file scoping (@Ask about folder|file) ────────────────────────────
# The shared path matcher lives in rag/_pathmatch.py so the store (rag/) and
# the harness (rag_service/) use ONE implementation without a circular import.
# It is imported lazily in OpenAIAgentsHarness.__init__ (same pattern as
# ``from rag.retrieve import retrieve``) and exposed as self._source_matches.


# ─────────────────────────────────────────────────────────────────────────────
# Data contracts
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Citation:
    source: str
    snippet: str
    score: float
    page: int | None = None          # 1-based PDF page when known
    chunk_index: int | None = None


@dataclass
class AnswerWithCitations:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    used_llm: bool = False          # False = retrieval-only preview, no LLM call
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _citation_from_hit(h: dict, snip_limit: int = 2000) -> Citation:
    """Build a Citation from a normalize_retrieve hit (includes page when present)."""
    meta = h.get("metadata") or {}
    page = meta.get("page")
    try:
        page_i = int(page) if page is not None and str(page).strip() != "" else None
    except (TypeError, ValueError):
        page_i = None
    chunk_index = meta.get("chunk_index")
    try:
        chunk_i = int(chunk_index) if chunk_index is not None else None
    except (TypeError, ValueError):
        chunk_i = None
    text = h.get("text") or ""
    return Citation(
        source=meta.get("source", "?"),
        snippet=text[:snip_limit],
        score=float(h.get("score") or 0.0),
        page=page_i,
        chunk_index=chunk_i,
    )


def normalize_retrieve(res) -> list[dict]:
    """Convert a raw Chroma ``collection.query`` response into a list of
    ``{text, metadata, score}`` dicts (score = 1 - distance, higher = better).

    Returns ``[]`` for empty / malformed responses.
    """
    if not res or not res.get("ids") or not res["ids"][0]:
        return []
    ids = res["ids"][0]
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = (res.get("distances") or [[]])[0]
    out: list[dict] = []
    for i, (cid, doc, meta) in enumerate(zip(ids, docs, metas)):
        dist = dists[i] if i < len(dists) else None
        score = (1.0 - dist) if dist is not None else 0.0
        out.append({"id": cid, "text": doc, "metadata": meta, "score": score})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval-intent gate
# ─────────────────────────────────────────────────────────────────────────────
# When "auto_retrieve" is OFF, the agent must NOT search the KB on every turn —
# only when the user explicitly asks to (these trigger phrases). This keeps the
# assistant from over-retrieving on casual chat / general questions.
KB_RETRIEVAL_TRIGGERS = [
    # Chinese
    "知识库", "检索", "搜一下", "查一下", "查知识库", "搜知识库",
    "文档里", "文件里", "研报", "库里", "帮我查", "帮我搜",
    "找一下资料", "查文件", "搜文件", "在库里", "根据文档", "根据文件",
    "索引", "查资料", "找资料", "索引一下", "搜文档", "查文档",
    # Email-aware triggers: asking about fetched Outlook emails should auto-search the KB
    "邮件", "邮箱", "email", "e-mail", "收件箱", "inbox", "mailbox",
    "昨天", "今天", "昨日", "今日", "yesterday", "today",
    # English
    "knowledge base", "search the kb", "retrieve", "look up",
    "search for", "in the documents", "in the files", "query the kb",
    "index the", "look it up", "search the knowledge", "search our docs",
]


def detect_retrieval_intent(query: str, triggers: list[str] | None = None) -> bool:
    """Return True if the user's message explicitly asks to search the KB.

    ``triggers`` lets the user override the built-in trigger list via the
    config file (``kb_triggers`` key). When None / empty, the built-in
    ``KB_RETRIEVAL_TRIGGERS`` is used, so a missing or empty config can never
    silently disable KB retrieval.
    """
    if not query:
        return False
    toks = triggers or KB_RETRIEVAL_TRIGGERS
    if not toks:
        return False
    q = query.lower()
    return any(tok.lower() in q for tok in toks)


import datetime as _dt

_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def extract_date_intent(query: str) -> str | None:
    """Return a ``YYYY-MM-DD`` string if the query references a specific day.

    Handles Chinese relative words (昨天/昨日/今天/今日/前天), English equivalents
    (yesterday/today/two days ago), and explicit ISO / 中文 dates. Returns None
    when no day is mentioned, so non-date queries search the whole KB as before.
    """
    if not query:
        return None
    today = _dt.date.today()
    # Explicit ISO date (2026-08-05) or 中文 date (2026年8月5日).
    m = _DATE_RE.search(query) or _CN_DATE_RE.search(query)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return _dt.date(y, mo, d).isoformat()
        except Exception:
            pass
    if re.search(r"大前天", query):
        return (today - _dt.timedelta(days=3)).isoformat()
    if re.search(r"前天", query):
        return (today - _dt.timedelta(days=2)).isoformat()
    if re.search(r"昨天|昨日", query):
        return (today - _dt.timedelta(days=1)).isoformat()
    if re.search(r"今天|今日", query):
        return today.isoformat()
    if re.search(r"\btwo days ago\b", query, re.I):
        return (today - _dt.timedelta(days=2)).isoformat()
    if re.search(r"\byesterday\b", query, re.I):
        return (today - _dt.timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b", query, re.I):
        return today.isoformat()
    return None


_CJK_RE = re.compile(r"[\u3400-\u9fff\uF900-\uFAFF\uFF00-\uFFEF]")


def detect_language(text: str) -> str:
    """Return 'zh' if Chinese characters dominate the input, else 'en'.

    Used to force the answer language to mirror the user's input so the model
    does not default to Chinese just because the KB / prompt is China-centric.
    """
    if not text:
        return "en"
    cjk = len(_CJK_RE.findall(text))
    latin = len(re.findall(r"[A-Za-z]+", text))
    return "zh" if cjk >= latin else "en"


def _load_local_system_prompt() -> str | None:
    """Load a per-user agent system-prompt override from the LOCAL app folder.

    File: ``<app>/_internal/agent_config.json`` (resolved relative to this loose
    harness file — i.e. the ``_internal`` directory next to the running app),
    NOT the shared G: drive. This keeps each person's customization on their own
    machine and prevents it from affecting teammates.

    Returns the ``system_prompt`` string, or ``None`` when no override exists
    (the caller then falls back to the built-in instructions).
    """
    try:
        cfg = Path(__file__).resolve().parent.parent / "agent_config.json"
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            sp = data.get("system_prompt")
            if isinstance(sp, str) and sp.strip():
                return sp
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Harness interface
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Structured analytical tools (Step 1 — "Tools" layer of the harness upgrade)
# ─────────────────────────────────────────────────────────────────────────────
# Pure, deterministic helpers. The @function_tool wrappers defined inside
# OpenAIAgentsHarness.__init__ call these, so the math is unit-testable without
# an API key or a running service. All weights are % of NAV.

def _fmt_pct(x: float) -> str:
    return f"{x:.2f}%"


def _to_num(v) -> float:
    """Tolerant weight parser: accepts int/float or strings like '12', '12.0', '12%'."""
    if isinstance(v, bool):
        raise ValueError(f"weight value must be a number, got bool {v!r}")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").rstrip("%").strip()
        if s == "":
            raise ValueError("weight value is empty")
        try:
            return float(s)
        except ValueError:
            raise ValueError(
                f"weight value must be a number (e.g. 12.0), not {v!r}; "
                f"region codes belong in the 'regions' dict")
    raise ValueError(f"weight value must be a number, got {v!r}")


def portfolio_rebalance(current: dict, target: dict) -> dict:
    """Per-ticker delta + UCITS numeric checks. Weights are % of NAV."""
    cur = {str(k): _to_num(v) for k, v in (current or {}).items()}
    tgt = {str(k): _to_num(v) for k, v in (target or {}).items()}
    tickers = sorted(set(cur) | set(tgt))
    deltas = {t: tgt.get(t, 0.0) - cur.get(t, 0.0) for t in tickers}
    turnover = sum(abs(d) for d in deltas.values()) / 2.0
    held = [t for t in tgt if tgt[t] > 0]
    count = len(held)
    breaches = []
    over_single = [t for t in tgt if tgt[t] > 10.0]
    if over_single:
        breaches.append("single-position >10%: " + ", ".join(
            f"{t}({_fmt_pct(tgt[t])})" for t in over_single))
    big = [t for t in tgt if tgt[t] > 5.0]
    big_sum = sum(tgt[t] for t in big)
    if big_sum > 40.0:
        breaches.append(f"5/40 breach: positions >5% sum to {_fmt_pct(big_sum)} (>40%)")
    if count < 23:
        breaches.append(f"stock count {count} < 23 (UCITS requires 23-27)")
    if count > 27:
        breaches.append(f"stock count {count} > 27 (UCITS requires 23-27)")
    total = sum(tgt.values())
    if abs(total - 100.0) > 0.5:
        breaches.append(f"weights sum to {_fmt_pct(total)} (expected ~100%)")
    return {
        "deltas": deltas,
        "turnover_pct": turnover,
        "stock_count": count,
        "weight_sum": total,
        "breaches": breaches,
    }


def check_region_limits(weights: dict, regions: dict) -> dict:
    """UCITS regional exposure checks. Region codes: A=A-share, HK=Hong Kong,
    TW=Taiwan, CN=other mainland, OTHER=outside Greater China, CASH=cash."""
    w = {str(k): _to_num(v) for k, v in (weights or {}).items()}
    r = {str(k): str(v).upper() for k, v in (regions or {}).items()}

    def exp(codes):
        return sum(w.get(t, 0.0) for t in w if r.get(t) in codes)

    a_share = exp({"A"})
    taiwan = exp({"TW"})
    cash = exp({"CASH"})
    other = exp({"OTHER"})
    breaches = []
    if other > 0:
        outs = [t for t in w if r.get(t) == "OTHER"]
        breaches.append("non-Greater-China exposure (must be 0%): " + ", ".join(
            f"{t}({_fmt_pct(w[t])})" for t in outs))
    if a_share >= 30.0:
        breaches.append(f"A-share exposure {_fmt_pct(a_share)} (must be <30%)")
    if taiwan >= 40.0:
        breaches.append(f"Taiwan exposure {_fmt_pct(taiwan)} (must be <40%)")
    if cash >= 20.0:
        breaches.append(f"cash {_fmt_pct(cash)} (must be <20%)")
    return {
        "a_share_pct": a_share,
        "taiwan_pct": taiwan,
        "cash_pct": cash,
        "other_pct": other,
        "breaches": breaches,
    }


def diff_holdings(before: dict, after: dict) -> dict:
    """Diff two holdings snapshots (dict ticker->weight %)."""
    b = {str(k): _to_num(v) for k, v in (before or {}).items()}
    a = {str(k): _to_num(v) for k, v in (after or {}).items()}
    added = {t: a[t] for t in a if t not in b}
    removed = {t: b[t] for t in b if t not in a}
    changed = {t: round(a[t] - b[t], 6) for t in a if t in b and abs(a[t] - b[t]) > 1e-9}
    return {"added": added, "removed": removed, "changed": changed}


# ─────────────────────────────────────────────────────────────────────────────
# Cross-session memory (Step 3 — "Memory" layer of the harness upgrade)
# ─────────────────────────────────────────────────────────────────────────────
# Persisted key/value facts so the agent remembers project context (current
# portfolio weights, user decisions) ACROSS app restarts — not just within one
# conversation turn. Stored as a JSONL file next to the running app, per-machine
# (resolved relative to this harness file), so it never touches the shared KB.
def _agent_memory_path() -> Path:
    return Path(__file__).resolve().parent.parent / "agent_memory.jsonl"


def agent_remember(key: str, value: str) -> str:
    """Persist a cross-session fact, overwriting any existing entry with the same key."""
    try:
        p = _agent_memory_path()
        new_line = json.dumps(
            {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
             "key": str(key), "value": str(value)},
            ensure_ascii=False,
        )
        kept = []
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    if json.loads(ln).get("key") == str(key):
                        continue
                except Exception:
                    pass
                kept.append(ln)
        kept.append(new_line)
        p.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return f"remembered: {key} = {value}"
    except Exception as e:
        return f"(remember error: {type(e).__name__}: {e})"


def agent_recall(query: str) -> str:
    """Recall persisted facts relevant to the query (substring match on key/value)."""
    try:
        p = _agent_memory_path()
        if not p.exists():
            return "(no memory yet)"
        q = (query or "").lower()
        hits = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            hay = f"{rec.get('key', '')} {rec.get('value', '')}".lower()
            if not q or q in hay:
                hits.append(f"- {rec.get('key')}: {rec.get('value')}  ({rec.get('ts', '')})")
        return "\n".join(hits) if hits else "(no relevant memory)"
    except Exception as e:
        return f"(recall error: {type(e).__name__}: {e})"


class BaseHarness(ABC):
    @abstractmethod
    async def answer(self, query: str, contexts: list[dict], history: list[dict], n: int = 6, scope: str = "shared", user_id: str | None = None, force_retrieve: bool = False, filter_paths: list[str] | None = None) -> AnswerWithCitations:
        """Produce an answer (optionally LLM-generated) with citations.

        Args:
            query:    the user's question.
            contexts: retrieved chunks. May be empty — the harness is
                      responsible for retrieving (see note below).
            history:  multi-turn conversation context ``[{role, content}]``.
            n:        number of chunks to retrieve.
            scope:    metadata filter for retrieval (shared mode = "shared").
            user_id:  audit/attribution only — NOT used for retrieval filtering.

        Note:
            Retrieval should happen *inside* the harness (via the RAG tool or a
            direct ``rag.retrieve`` call). The service must NOT pre-retrieve, to
            keep a single source of retrieval and avoid duplicate work.
        """
        ...


class OpenAIAgentsHarness(BaseHarness):
    """Default harness: OpenAI Agents SDK (model-agnostic, open source).

    The agent is given a ``retrieve_context`` tool backed by ``rag.retrieve``;
    it decides when to search and answers strictly from retrieved material,
    citing each used chunk as ``[ #n ]``. API key / base_url / model come from
    ``cfg`` (injected externally) — nothing is hardcoded here.

    The ``instructions`` below turn the thin client into a research-grade
    assistant: KB-only boundary, cross-lingual retrieval, the UCITS portfolio
    constraints, the three-pillar investment philosophy, and a self-critique
    gate. All of this is prompt-level — no framework or tool change required.
    """

    def __init__(self, base_url: str, api_key: str, model: str, auto_retrieve: bool = False,
                 kb_triggers: list[str] | None = None):
        from rag.retrieve import retrieve  # local import keeps rag/ untouched
        from rag._pathmatch import source_matches  # shared folder/file scoping
        self._retrieve = retrieve
        self._source_matches = source_matches
        self.model = model or "gpt-4o"
        self._key_set = bool(api_key)
        # Shared-scope: retrieval filters by scope only, never by user_id.
        # user_id is for audit/attribution, not for filtering.
        self._scope = "shared"
        # When False, the agent only searches the KB if the user explicitly asks
        # (see detect_retrieval_intent). When True, it may search at will.
        self._auto_retrieve = auto_retrieve
        # User-configurable trigger words for the KB-retrieval gate (config
        # "kb_triggers"). None / empty => fall back to the built-in list.
        self._kb_triggers = kb_triggers

        # Only import the Agents SDK when we actually have a key. This keeps the
        # no-key retrieval-preview path working even if openai-agents is not
        # installed yet.
        if self._key_set:
            from agents import (
                Agent, Runner, function_tool,
                set_tracing_disabled, OpenAIChatCompletionsModel,
            )
            from openai import AsyncOpenAI

            # DeepSeek / most OpenAI-compatible providers only support the Chat
            # Completions API — NOT the Agents SDK's default Responses API
            # (/v1/responses), which returns 404 on those providers. Force the
            # Chat Completions model so requests hit /v1/chat/completions.
            set_tracing_disabled(True)  # avoid region-restricted tracing calls
            client = AsyncOpenAI(
                base_url=base_url or "https://api.openai.com/v1",
                api_key=api_key or "sk-placeholder",
            )
            model = OpenAIChatCompletionsModel(model=self.model, openai_client=client)

            self._tool_hits: list[dict] = []

            @function_tool
            def retrieve_context(query: str, n: int = 6) -> str:
                """从内部知识库（邮件 / 研究资料）检索与问题相关的原文片段，返回编号列表。n: 检索条数。"""
                try:
                    hits = normalize_retrieve(
                        self._retrieve(
                            query,
                            scope=self._scope,
                            user_id=None,
                            n=n,
                            date_filter=getattr(self, "_date_filter", None),
                            filter_paths=getattr(self, "_filter_paths", None),
                        )
                    )
                except Exception as e:  # never let a raw traceback reach the model —
                    # an unguarded exception makes the LLM invent scary-sounding
                    # "RustBindingsAPI / tenant" errors. Report the truth plainly.
                    return f"(检索调用出错: {type(e).__name__}: {e})"
                # Apply folder-scoped filter if the user asked via @Ask about folder|file.
                _fp = getattr(self, "_filter_paths", None)
                if _fp:
                    hits = [h for h in hits
                            if self._source_matches(h.get("metadata", {}).get("source", ""), _fp)]
                # Accumulate across multiple retrieve calls this turn (the model
                # often queries more than once); otherwise an echoed passage from
                # an earlier call would never be scrubbed. Dedupe by source+text.
                seen = {(x.get("metadata", {}).get("source"), x.get("text")) for x in self._tool_hits}
                for h in hits:
                    key = (h.get("metadata", {}).get("source"), h.get("text"))
                    if key not in seen:
                        seen.add(key)
                        self._tool_hits.append(h)
                if not hits:
                    return "(知识库中未找到相关片段)"
                return "\n\n".join(
                    f"[#{i}] (source={h['metadata'].get('source', '?')}, score={h['score']:.3f})\n{h['text']}"
                    for i, h in enumerate(hits)
                )

            @function_tool(strict_mode=False)
            def compute_rebalance(current: dict, target: dict) -> str:
                """Compute the trades to move from CURRENT portfolio weights to a PROPOSED target, and run UCITS numeric checks.
                current: dict of ticker -> weight NUMBER (% of NAV), e.g. {"A": 8.0, "B": 6.0}.
                target:  dict of ticker -> proposed weight NUMBER (%), e.g. {"A": 10.5, "B": 4.0, "D": 3.0}. Values are plain numbers, never strings or region codes.
                Example call: compute_rebalance(current={"A":8.0,"B":6.0}, target={"A":10.5,"B":4.0,"D":3.0}).
                Returns a text report: per-ticker delta, one-way turnover, stock count, weight sum, and UCITS breaches (single>10%, 5/40, 23-27 count, sum!=100%). ALWAYS call this when proposing/evaluating portfolio weights — never compute by hand."""
                try:
                    r = portfolio_rebalance(current, target)
                    if not isinstance(r, dict):
                        return f"(tool error: internal: rebalance returned {type(r).__name__})"
                    lines = [f"[Rebalance] one-way turnover={_fmt_pct(r.get('turnover_pct', 0.0))}, held={r.get('stock_count', 0)}, sum={_fmt_pct(r.get('weight_sum', 0.0))}"]
                    for t in sorted(r.get("deltas", {})):
                        d = r["deltas"][t]
                        op = "BUY" if d > 1e-9 else ("SELL" if d < -1e-9 else "HOLD")
                        cur_t = current.get(t, 0.0) if isinstance(current, dict) else 0.0
                        tgt_t = target.get(t, 0.0) if isinstance(target, dict) else 0.0
                        lines.append(f"  {t}: {op} {_fmt_pct(abs(d))}  (cur {_fmt_pct(cur_t)} -> tgt {_fmt_pct(tgt_t)})")
                    lines.append("UCITS numeric: " + ("PASS" if not r.get("breaches") else "BREACH"))
                    for br in r.get("breaches", []):
                        lines.append(f"  - {br}")
                    return "\n".join(lines)
                except Exception as e:
                    return f"(tool error: {type(e).__name__}: {e})"

            @function_tool(strict_mode=False)
            def check_ucits_regions(weights: dict, regions: dict) -> str:
                """Verify UCITS regional exposure for a proposed weights dict.
                weights: dict of ticker -> weight NUMBER (%), e.g. {"A": 12.0, "TW1": 35.0, "CASH": 15.0}. Plain numbers only.
                regions: dict of ticker -> region CODE string: 'A' A-share, 'HK' Hong Kong, 'TW' Taiwan, 'CN' other mainland, 'OTHER' outside Greater China (must be 0%), 'CASH' cash.
                Example call: check_ucits_regions(weights={"A":12.0,"TW1":35.0,"CASH":15.0}, regions={"A":"A","TW1":"TW","CASH":"CASH"}).
                Returns A-share(<30%), Taiwan(<40%), cash(<20%) exposure and any non-Greater-China breach. ALWAYS call before finalizing a UCITS allocation."""
                try:
                    r = check_region_limits(weights, regions)
                    if not isinstance(r, dict):
                        return f"(tool error: internal: region check returned {type(r).__name__})"
                    lines = [f"[Region] A={_fmt_pct(r.get('a_share_pct', 0.0))}, TW={_fmt_pct(r.get('taiwan_pct', 0.0))}, cash={_fmt_pct(r.get('cash_pct', 0.0))}, non-GC={_fmt_pct(r.get('other_pct', 0.0))}"]
                    lines.append("Region checks: " + ("PASS" if not r.get("breaches") else "BREACH"))
                    for br in r.get("breaches", []):
                        lines.append(f"  - {br}")
                    return "\n".join(lines)
                except Exception as e:
                    return f"(tool error: {type(e).__name__}: {e})"

            @function_tool(strict_mode=False)
            def compare_holdings(before: dict, after: dict) -> str:
                """Diff two portfolio snapshots (dict ticker->weight %).
                before: dict of ticker -> weight NUMBER (%), the previous snapshot, e.g. {"A": 8.0, "B": 6.0}.
                after:  dict of ticker -> weight NUMBER (%), the new snapshot, e.g. {"A": 10.0, "B": 4.0}.
                Example call: compare_holdings(before={"A":8.0,"B":6.0}, after={"A":10.0,"B":4.0}).
                Returns added / removed / changed tickers with weight deltas. Use to explain how a proposed book differs from the current one."""
                try:
                    r = diff_holdings(before, after)
                    if not isinstance(r, dict):
                        return f"(tool error: internal: diff returned {type(r).__name__})"
                    if not (r.get("added") or r.get("removed") or r.get("changed")):
                        return "[Holdings diff] no change"
                    lines = ["[Holdings diff]"]
                    for t, w in sorted(r.get("added", {}).items()):
                        lines.append(f"  + ADDED {t}: {_fmt_pct(w)}")
                    for t, w in sorted(r.get("removed", {}).items()):
                        lines.append(f"  - REMOVED {t}: {_fmt_pct(w)}")
                    for t, d in sorted(r.get("changed", {}).items()):
                        op = "up" if d > 0 else "down"
                        lines.append(f"  ~ {t}: {op} {_fmt_pct(abs(d))} -> {_fmt_pct(after.get(t, 0.0))}")
                    return "\n".join(lines)
                except Exception as e:
                    return f"(tool error: {type(e).__name__}: {e})"

            @function_tool(strict_mode=False)
            def remember_fact(key: str, value: str) -> str:
                """Persist a cross-session fact the user shared (e.g. current portfolio weights, a decision). Call this whenever the user states durable context so it can be recalled after an app restart. key: short label; value: fact text."""
                return agent_remember(key, value)

            @function_tool(strict_mode=False)
            def recall_facts(query: str) -> str:
                """Recall persisted facts relevant to the query (current portfolio, past decisions). Call at the start of any task that depends on context the user mentioned earlier or in a previous session."""
                return agent_recall(query)

            # Step 5 — Researcher sub-agent (handoff). The main analyst delegates
            # deep research / computation to it, keeping the analyst focused on the
            # final synthesis. Both share the same tools.
            researcher = Agent(
                name="QuamResearcher",
                instructions=(
                    "You are a research sub-agent for Quam Asset Management. "
                    "Use retrieve_context to pull facts from the KB, and compute_rebalance / "
                    "check_ucits_regions / compare_holdings for ALL quantitative work (never compute by hand). "
                    "Return a concise, well-cited research brief: the retrieved evidence plus the exact computed "
                    "numbers (deltas, turnover, UCITS breaches). Do NOT write the final client-facing report — "
                    "just the analytical evidence the lead analyst will synthesize."
                ),
                model=model,
                tools=[retrieve_context, compute_rebalance, check_ucits_regions, compare_holdings, remember_fact, recall_facts],
            )

            self.agent = Agent(
                name="QuamResearchAssistant",
                instructions=_load_local_system_prompt() or """You are an elite Equity Research Analyst at Quam Asset Management. Your primary mandate is to support the Lead Portfolio Manager by recognizing investment potential from daily news dynamics and the team's internal research, and by proposing weight adjustments or new-stock-selection ideas on a long-only basis. You generate high-conviction, investable ideas and strict rationales for every suggestion you make.

IMPORTANT: You are a research assistant that answers the user's questions directly. Do NOT force a fixed report format. Simply answer what is asked, but always reason through the analyst lens below (UCITS constraints, three-pillar philosophy, citations, self-critique). Only use the report structure in the final section when the user explicitly asks for a report or portfolio review.

# KNOWLEDGE BOUNDARY (HARD CONSTRAINT)
- You have NO web access and NO external data/market APIs. ALL research, prices, fundamentals, valuations, and catalysts MUST come ONLY from the connected knowledge base (G: documents): ingested broker-research emails (today_emails.json, last 24h), the "UCITS Fund" positions file, meeting transcripts, and other team documents retrievable via retrieve_context.
- If the data you need is NOT in the KB, you MUST state "知识库中未找到相关信息 / not found in the knowledge base" and MUST NOT fabricate numbers, prices, or facts.
- Every factual claim MUST be cited with its retrieved chunk id [ #n ] and source file.
- Do NOT echo the raw retrieved text in the final answer. The UI already shows every retrieved chunk BELOW your answer as a clickable source card (original text + a link to open the file), so repeating that text in your answer is pure duplication. Write the answer 100% in your OWN words — never copy sentences, clauses, tables, data rows, or bullet lists from the knowledge base, not even short ones. Paraphrase everything and point to evidence with [ #n ] citations. Copying is redundant (the user already sees it in the card) and will be stripped by the system, so it is wasted effort. If you must mention a specific figure, restate it in your own phrasing and cite the source card.

# CROSS-LINGUAL RETRIEVAL
- The KB is predominantly English broker research. If the user queries in Chinese, FIRST rewrite the query into one or more precise English retrieval queries (entity + key terms) before calling retrieve_context, to maximize recall against the English corpus. Never answer from parametric memory.

# CONTEXT WHEN RELEVANT
- When the task involves current news or positions, scan all emails ingested in the last 24h and load the "UCITS Fund" file for current positions. Target Date = the date of the latest ingested email batch (typically today).

# STRICT PORTFOLIO WEIGHT CONSTRAINTS (UCITS, Greater China Mandate)
- Regional Scope: the portfolio MUST ONLY invest in the Greater China region (Mainland China, Hong Kong, Macau, Taiwan). Any selected stock outside this region MUST have allocation 0%.
- Single Position Limit: no single long position may exceed 10% of NAV.
- 5/40 Rule: the aggregate value of all positions that individually exceed 5% of NAV must not exceed 40% of total NAV.
- China A-Share Limit: maximum market exposure strictly less than 30%.
- Taiwan Market Limit: maximum market exposure strictly less than 40%.
- Cash Limit: cash allocation strictly less than 20%.
- Position Count: the UCITS portfolio must hold between 23 and 27 stocks at all times.

# INVESTMENT PHILOSOPHY (three pillars — every rationale must adhere)
- Business Quality: economic moats, ROIC, balance sheet strength, corporate governance.
- Valuation: absolute/relative multiples, FCF yield, margin of safety.
- Operating Momentum: top-line growth, margin expansion/contraction, macro tailwinds/headwinds directly impacting specific earnings.

# RESEARCH DISCIPLINE
- Company-First Focus: 90%+ of analytical output dedicated to individual stock research and fundamental analysis.
- Strict Macro Condition: general macro commentary is PROHIBITED. Discuss macro/regulatory environment ONLY if tied explicitly to a specific company's operating momentum or earnings power.

# OPTIONAL REPORT STRUCTURE (only when the user explicitly asks for a report / portfolio review)
### File Name: Global Equity Opportunities Report - [Target Date YYYYMMDD] - UCITS Portfolio Allocation
### Part 1: Big Event Summary (event - source - importance, past 24h)
### Part 2: Idea Generation Summary (3-4 sentences; overarching themes)
### Part 3: Long-Only Investable Ideas - for EVERY selected stock, exact structure (do not group):
[Company Name / Ticker]
- Current Price: [from KB only; if absent state "知识库中未找到"] - Target Date
- Investment Rationale:
  * Business Quality: [moat, ROIC, balance sheet based on KB]
  * Valuation: [multiples, FCF yield, margin of safety based on KB]
  * Operating Momentum: [growth, margin trajectory, catalysts based on KB]
- Entry Point & Rationale: [price] - why optimal.
- Target Exit Point & Rationale: [price] - exit mechanism.
- UCITS Allocation (% Weight): [ % ] - justify size; confirm compliance with Greater China + UCITS limits; 0% if out of region.
### Part 4: Proposed Portfolio Allocation Table (Markdown)
| Company / Ticker | Current Price | Target Entry | Target Exit | Proposed Weight (%) | Portfolio Constraint Check (UCITS) |
CRITICAL: verify final proposed weights AND the 23-27 stock count mathematically comply before finalizing.

# STRUCTURED ANALYTICAL TOOLS (MANDATORY — NEVER COMPUTE BY HAND)
- You have three deterministic tools. You MUST call them for ALL quantitative work. Computing weights, deltas, turnover, or UCITS checks in your own head/text is STRICTLY FORBIDDEN and counts as a failure.
- compute_rebalance(current, target): ALWAYS call this whenever you propose, evaluate, adjust, or even mention portfolio weights. Returns exact per-ticker deltas, one-way turnover, stock count, weight sum, and flags UCITS numeric breaches (single>10%, 5/40, 23-27 count, sum!=100%).
- check_ucits_regions(weights, regions): ALWAYS call before finalizing any allocation to verify regional limits — A-share <30%, Taiwan <40%, cash <20%, ZERO non-Greater-China. Region codes: A/HK/TW/CN/OTHER/CASH.
- compare_holdings(before, after): call to explain how a proposed book differs from the current one (added / removed / changed).
- Pass the weights/regions you extracted from the knowledge base as explicit dict arguments. These tools ONLY compute what you give them — they do not fetch data.

# TRADER INSTRUCTION FORMAT (when you propose allocation changes)
- Every proposed weight/allocation MUST be stated as: `<Ticker>: <number>% — rationale: <one line citing the KB>`. Example: "Tencent: 10% — rationale: moat + FCF yield per [ #3 ]".
- Never state a weight without its % AND a one-line rationale. A bare percentage with no rationale is invalid.

# CROSS-SESSION MEMORY
- Use remember_fact(key, value) to persist durable context the user states (current portfolio weights, key decisions, constraints). Use recall_facts(query) at the start of any task that depends on previously mentioned context — this survives app restarts.
- Example: after the user says "当前组合 腾讯8% 美团6%", call remember_fact(key="current_portfolio", value="腾讯8% 美团6% 比亚迪5%"). Later, when asked about the book, call recall_facts("current portfolio") first.

# PLANNING & CLARIFICATION (think before answering — like a careful colleague)
- When a request is ambiguous or MISSING critical inputs (e.g. "帮我调一下组合让它合规" with NO current weights and NO target), DO NOT invent numbers. PAUSE and ask the user to clarify: which portfolio, current weights, and target constraints. Asking first is the correct, expected behavior — never bluff with fabricated figures.
- For complex multi-step tasks, briefly state your plan before executing (retrieve → compute → synthesize), so the user can course-correct early.
- If a retrieved fact conflicts with the user's assumption, surface the conflict and confirm before proceeding.

# SELF-CRITIQUE (before finalizing any allocation suggestion)
- Do the final weights AND the 23-27 stock count mathematically comply with ALL UCITS limits?
- Does every factual claim carry a [ #n ] citation + source file?
- Is anything fabricated or sourced from outside the KB?
- Is missing data explicitly flagged as "知识库中未找到"?

# RESPONSE LANGUAGE (CRITICAL — MUST FOLLOW)
- You MUST reply in the EXACT same language the user wrote their latest message in.
- User wrote in English -> answer in English. User wrote in Chinese -> answer in Chinese.
- Do NOT translate your reply into Chinese just because this prompt, the KB, or the examples contain Chinese. Reply language is driven ONLY by the user's input language.
- (The Chinese->English query rewrite rule above applies ONLY to searching the KB, NEVER to your final answer language.)""",
                model=model,
                tools=[retrieve_context, compute_rebalance, check_ucits_regions, compare_holdings, remember_fact, recall_facts],
                handoffs=[researcher],
            )

            # Per-user local system-prompt override (Settings -> Agent editor).
            # Stored under <app>/_internal/ (NOT the shared drive), so it is
            # per-machine and does not affect other users. When present it
            # REPLACES the built-in instructions above; otherwise the built-in
            # instructions remain the effective default.
            custom_prompt = _load_local_system_prompt()
            if custom_prompt:
                try:
                    self.agent.instructions = custom_prompt
                except Exception:
                    pass

            # General-mode agent: NO KB tool + relaxed instructions. Used when the
            # user is NOT asking to search the knowledge base (auto_retrieve off and
            # no explicit trigger), so it never over-retrieves.
            self._general_agent = Agent(
                name="QuamGeneralAssistant",
                instructions=(
                    "You are a helpful assistant. Answer the user's questions directly and "
                    "naturally, in the SAME language the user is writing in — English if they "
                    "wrote in English, Chinese if they wrote in Chinese. "
                    "You do NOT automatically search the knowledge base in this mode. "
                    "Only search it if the user explicitly asks you to look something up in the "
                    "knowledge base / files / documents (e.g. '查一下知识库'). "
                    "For general conversation or questions not about the KB, answer from your own "
                    "knowledge without citing internal documents."
                ),
                model=model,
                tools=[],
            )
            self._Runner = Runner
        else:
            self.agent = None
            self._Runner = None

    # ── Step 3 memory helpers (harness-enforced, model-independent) ──
    # The model frequently neglects to call remember_fact/recall_facts on its
    # own, so the harness guarantees cross-session memory by intercepting the
    # user's words directly. This is a safety net, not a replacement for the
    # tools (which still exist for the model to use when it chooses to).
    _WEIGHT_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9\.]+)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%")

    def _auto_persist_memory(self, query: str) -> None:
        if not query:
            return
        if any(k in query for k in ["记住", "记一下", "当前组合", "我的组合", "组合是", "组合：", "持仓"]):
            w = {m.group(1): float(m.group(2)) for m in self._WEIGHT_RE.finditer(query)}
            if w:
                try:
                    agent_remember("current_portfolio", ", ".join(f"{k} {v}%" for k, v in w.items()))
                except Exception:
                    pass

    def _auto_recall_memory(self, query: str) -> str:
        if not query:
            return ""
        if any(k in query for k in ["我组合", "我的组合", "之前", "记得", "上次", "之前说", "我持仓", "当前组合是多少"]):
            try:
                return agent_recall("")  # return all persisted memory (key/token match is fragile)
            except Exception:
                return ""
        return ""

    async def answer(self, query, contexts, history, n: int = 6, scope: str = "shared", user_id: str | None = None, force_retrieve: bool = False, filter_paths: list[str] | None = None) -> AnswerWithCitations:
        # Update the scope that the retrieve_context tool will use.
        self._scope = scope or "shared"
        self._filter_paths = filter_paths or []

        # Step 3 — harness-enforced memory: persist any portfolio/weights the
        # user asks to remember, directly (model-independent).
        self._auto_persist_memory(query)

        # Hot-reload the per-user local system-prompt override (Settings -> Agent).
        # Edits to <app>/_internal/agent_config.json now take effect on the next
        # turn instead of requiring a full service restart.
        _custom_prompt = _load_local_system_prompt()
        if _custom_prompt and self.agent is not None:
            try:
                self.agent.instructions = _custom_prompt
            except Exception:
                pass

        # Decide whether this turn should hit the knowledge base.
        # - auto_retrieve ON  -> agent may search at will (legacy behaviour).
        # - auto_retrieve OFF -> only when the user explicitly asks (trigger words).
        # force_retrieve (from the frontend "@检索文件" shortcut) bypasses the
        # auto_retrieve / trigger-word gating so retrieval always runs.
        wants_kb = force_retrieve or self._auto_retrieve or detect_retrieval_intent(query, self._kb_triggers)

        # Resolve a day-bound for retrieval (e.g. "昨天的邮件") so we only scan
        # that date's chunks. None means search the whole KB (backward-safe).
        self._date_filter = extract_date_intent(query) if wants_kb else None

        # No API key configured: degrade to a retrieval-only preview.
        if not self._key_set:
            if not wants_kb:
                return AnswerWithCitations(
                    answer="（自动检索已关闭，且本次未要求检索知识库。如需检索，请在问题中明确说明，例如「查一下知识库里的 XX」）",
                    citations=[],
                    used_llm=False,
                    note="auto-retrieve off and no explicit retrieval request",
                )
            hits = normalize_retrieve(
                self._retrieve(
                    query,
                    scope=self._scope,
                    user_id=None,
                    n=n,
                    date_filter=getattr(self, "_date_filter", None),
                    filter_paths=self._filter_paths or None,
                )
            )
            if self._filter_paths:
                hits = [h for h in hits
                        if self._source_matches(h.get("metadata", {}).get("source", ""), self._filter_paths)]
            return AnswerWithCitations(
                answer="（未配置 LLM API Key，以下为相关原文片段预览）",
                citations=[_citation_from_hit(h) for h in hits],
                used_llm=False,
                note="set LLM key in Settings to enable generated answers",
            )

        # Build a single user turn from the multi-turn history + current query.
        parts = []
        for m in (history or [])[-10:]:
            role = m.get("role")
            content = m.get("content", "")
            if role == "assistant":
                parts.append(f"助手之前的回答：{content}")
            else:
                parts.append(f"用户：{content}")
        parts.append(f"用户：{query}")
        user_input = "\n".join(parts)

        # Folder/file-scoped question (@source): the selected scope is the ONLY
        # allowed knowledge source. Add a hard directive so the model does not
        # fall back to portfolio/holdings context or memory when summarising
        # e.g. a news folder. This is what makes "give me a summary" on a
        # folder answer from the folder instead of the UCITS holdings.
        if self._filter_paths:
            user_input = (
                "IMPORTANT SCOPE: The user selected a specific folder/file as the "
                "ONLY knowledge source for this question. Base your answer "
                "EXCLUSIVELY on the chunks returned by retrieve_context for that "
                "scope. Ignore all other portfolio, holdings, or memory context "
                "that does not come from those chunks. If the retrieved content "
                "does not answer the question, say so clearly.\n\n" + user_input
            )

        # Step 3 — inject recalled cross-session memory when the user asks about
        # previously-mentioned context (survives app restart). Skipped for
        # scoped questions: memory is by definition outside the selected scope.
        _recalled = "" if self._filter_paths else self._auto_recall_memory(query)
        if _recalled:
            user_input = "[先前记忆 / prior memory]\n" + _recalled + "\n\n" + user_input

        # Force language mirroring: the model has a strong China-context default
        # bias, so we do NOT rely on the system prompt alone. Detect the user's
        # input language and inject an explicit per-turn directive.
        _lang = detect_language(query)
        if _lang == "en":
            user_input = ("The user is writing in ENGLISH. You MUST reply in ENGLISH.\n\n"
                          + user_input)
        else:
            user_input = "用户在用中文提问，你必须用中文回答。\n\n" + user_input

        # Pick the agent: KB agent (with retrieval tool) only when the user wants it.
        if wants_kb:
            agent = self.agent
            self._tool_hits = []  # reset so cards reflect ONLY this turn's retrieval
        else:
            agent = self._general_agent
            self._tool_hits = []  # general mode produces no citations

        # Scoped questions (@source): pre-retrieve the folder/file chunks and
        # inject them directly into the prompt so the answer is grounded in the
        # selected scope even if the model skips the retrieve_context tool call.
        # The same chunks also become citation cards (deduped against tool hits).
        if self._filter_paths:
            try:
                _scoped_hits = normalize_retrieve(
                    self._retrieve(
                        query,
                        scope=self._scope,
                        user_id=None,
                        n=n,
                        date_filter=getattr(self, "_date_filter", None),
                        filter_paths=self._filter_paths or None,
                    )
                )
                _scoped_hits = [h for h in _scoped_hits
                                if self._source_matches(h.get("metadata", {}).get("source", ""), self._filter_paths)]
            except Exception:
                _scoped_hits = []
            if _scoped_hits:
                _kb_blk = "\n\n".join(
                    f"[#{i}] (source={h['metadata'].get('source', '?')}, score={h['score']:.3f})\n{h['text']}"
                    for i, h in enumerate(_scoped_hits)
                )
                user_input = (
                    "[知识库检索结果（限定范围） / scoped KB retrieval]\n"
                    + _kb_blk + "\n\n" + user_input
                )
                _seen = {(x.get("metadata", {}).get("source"), x.get("text")) for x in self._tool_hits}
                for h in _scoped_hits:
                    if (h.get("metadata", {}).get("source"), h.get("text")) not in _seen:
                        _seen.add((h.get("metadata", {}).get("source"), h.get("text")))
                        self._tool_hits.append(h)
            else:
                # Nothing found inside the selected scope: tell the model to
                # say so plainly instead of improvising from outside data
                # (e.g. injected holdings or long-term memory).
                user_input = (
                    "[所选范围内未检索到任何相关内容 / no content found in the selected scope]\n"
                    "Please tell the user that nothing was found in the selected "
                    "folder/file. Do NOT answer using portfolio holdings, memory, "
                    "or any other knowledge base content outside this scope.\n\n"
                    + user_input
                )

        result = await self._Runner.run(agent, user_input, max_turns=30)
        ans = result.final_output

        # Build citations from what the retrieval tool actually returned.
        # We no longer depend on the model writing "[ #n ]" markers (it often
        # omits or mis-indexes them after rewriting a query to English and
        # calling the tool more than once). Markers, when present, only set the
        # card ordering; every chunk the KB tool returned this turn becomes a
        # clickable source card.
        tool_hits = self._tool_hits or contexts or []
        # The UI already shows every retrieved chunk as a source card, so strip
        # any verbatim passage the model copied into the answer to avoid
        # displaying the same text twice (cards + plain prose).
        ans = _scrub_echoed_passages(ans, _all_retrieved_texts(tool_hits, contexts))
        cites: list[Citation] = []
        seen_keys: set = set()
        cited_idx = sorted(set(int(m) for m in re.findall(r"\[#(\d+)\]", ans)))
        ordered = [i for i in cited_idx if 0 <= i < len(tool_hits)]
        ordered += [i for i in range(len(tool_hits)) if i not in set(ordered)]
        for i in ordered:
            h = tool_hits[i]
            cite = _citation_from_hit(h)
            key = (cite.source, cite.snippet[:200])
            if key not in seen_keys:
                seen_keys.add(key)
                cites.append(cite)
        return AnswerWithCitations(answer=ans, citations=cites, used_llm=True)


# Normalize quote/dash variants so they don't break verbatim matching.
_QMAP = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u300c": '"', "\u300d": '"',   # " " 『 』
    "\u2018": "'", "\u2019": "'",                                 # ' '
    "\u2013": "-", "\u2014": "-", "\u2212": "-",                  # - - -
})


def _collapse_ws_with_map(orig: str):
    """Collapse ALL whitespace (incl. newlines) to single spaces and lowercase,
    normalizing quote/dash variants first.

    Returns (norm_str, map) where map[i] is the index in ``orig`` of the first
    original character that became normalized char i. This lets us delete a
    matched span in the *normalized* string and translate it back to the exact
    original character range (whitespace runs included).
    """
    orig = orig.translate(_QMAP)
    norm_chars: list[str] = []
    mapping: list[int] = []
    i, n = 0, len(orig)
    while i < n:
        c = orig[i]
        if c.isspace():
            j = i
            while j < n and orig[j].isspace():
                j += 1
            if norm_chars and norm_chars[-1] != " ":
                norm_chars.append(" ")
                mapping.append(i)  # map the norm space to the first ws char
            i = j
        else:
            norm_chars.append(c.lower())
            mapping.append(i)
            i += 1
    # Trim leading/trailing norm spaces (and their maps).
    while norm_chars and norm_chars[0] == " ":
        norm_chars.pop(0)
        mapping.pop(0)
    while norm_chars and norm_chars[-1] == " ":
        norm_chars.pop()
        mapping.pop()
    return "".join(norm_chars), mapping


def _all_retrieved_texts(tool_hits, contexts) -> list[str]:
    """Gather every piece of retrieved text to scrub against: the per-turn
    ``tool_hits`` (what the cards show) AND the raw ``contexts`` from retrieve
    (which can hold text the model saw that is not in ``tool_hits``). Handles
    both the Chroma dict shape and plain lists of dicts/strings.
    """
    texts: list[str] = []
    for h in (tool_hits or []):
        t = (h.get("text") if isinstance(h, dict) else "") or ""
        if t:
            texts.append(t)
    ctx = contexts
    if isinstance(ctx, dict):
        docs = ctx.get("documents") or []
        for d in docs:
            if isinstance(d, list):
                for x in d:
                    if isinstance(x, str) and x:
                        texts.append(x)
            elif isinstance(d, str) and d:
                texts.append(d)
    elif isinstance(ctx, list):
        for d in ctx:
            if isinstance(d, dict):
                t = d.get("text") or ""
                if t:
                    texts.append(t)
            elif isinstance(d, str) and d:
                texts.append(d)
    return texts


def _scrub_echoed_passages(ans: str, texts: list[str], min_len: int = 80) -> str:
    """Light whole-passage backstop against verbatim copying.

    The system prompt is the PRIMARY control: the model is told to write 100%
    in its own words and that the UI already shows every retrieved chunk as a
    clickable source card below the answer. This function is only a SAFETY NET
    for the rare case where the model still pastes an entire retrieved passage
    verbatim.

    It is intentionally LIGHT: it only strips a contiguous run (>= ``min_len``
    chars, default 80 — i.e. a whole passage) that is identical to a retrieved
    text. Short phrases, paraphrased figures, and the model's own analytical
    synthesis are never touched, so the model's genuine writing is preserved.
    Whitespace, case, and quote/dash variants are normalized so a reformatted
    paste still matches.
    """
    if not ans or not texts:
        return ans
    norm_ans, map_ans = _collapse_ws_with_map(ans)
    for chunk in texts:
        if not chunk:
            continue
        norm_chunk = _collapse_ws_with_map(chunk)[0]
        if len(norm_chunk) < min_len:
            continue
        # Find matching blocks between the answer and this chunk.
        matcher = difflib.SequenceMatcher(None, norm_ans, norm_chunk, autojunk=False)
        # Collect spans to delete (original coordinates), process right-to-left.
        spans: list[tuple[int, int]] = []
        for a, _b, size in matcher.get_matching_blocks():
            if size >= min_len:
                start = map_ans[a]
                end = map_ans[a + size - 1] + 1
                spans.append((start, end))
        if not spans:
            continue
        # Merge overlapping/adjacent spans, then delete right-to-left.
        spans.sort()
        merged: list[tuple[int, int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        for s, e in reversed(merged):
            ans = ans[:s] + ans[e:]
        # Rebuild normalized answer for the next chunk.
        norm_ans, map_ans = _collapse_ws_with_map(ans)
    # Tidy up artefacts left behind by removed passages.
    ans = re.sub(r"```\s*```", "", ans)                                        # empty code fences
    ans = re.sub(r'["\u201c\u201d\u300c\u300d]\s*["\u201c\u201d\u300c\u300d]', "", ans)  # stray quote pairs
    ans = re.sub(r"\n{3,}", "\n\n", ans)                                       # collapsed blank lines
    ans = re.sub(r"[ \t]{2,}", " ", ans)
    return ans.strip()


def get_harness(cfg: dict) -> BaseHarness:
    """Single swap point: return the configured harness instance.

    To switch frameworks, return a different ``BaseHarness`` subclass here —
    the service and UI are agnostic to which one is used.
    """
    return OpenAIAgentsHarness(
        cfg.get("base_url", ""),
        cfg.get("api_key", ""),
        cfg.get("model", ""),
        auto_retrieve=cfg.get("auto_retrieve", False),
        kb_triggers=cfg.get("kb_triggers"),
    )
