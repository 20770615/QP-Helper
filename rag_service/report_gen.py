"""Daily-report generation for the Quam UCITS email assistant.

This module is the *intelligence* half of the "Generate Report" skill. It takes
the already-fetched ``today_emails.json`` + the latest UCITS holdings CSV and
asks the configured LLM (same ``base_url`` / ``api_key`` / ``model`` as the RAG
harness) to produce the report **body** as Markdown. The DOCX cover + styling
are added later by :mod:`report_docx`.

Why a dedicated module (not the RAG agent)?
    Report generation is *not* a retrieval-QA task — it must read the full set
    of fetched emails and the holdings CSV verbatim, not "retrieve top-k chunks".
    Reusing the agent's ``retrieve_context`` tool would lose emails and add
    citation noise. So we do one focused chat completion with the complete
    payload. The LLM client is built the same way the harness builds it
    (OpenAI-compatible Chat Completions), keeping a single config contract.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# ── System prompts (the "core asset") ───────────────────────────────────────
SYSTEM_PROMPT_ZH = """你是 Quam Asset Management 的精英股票研究分析师，负责支持 Lead PM 产出高确信度的长线做多想法。

请基于今天抓取的 Outlook 邮件（已抓取完成，内容在用户消息中）以及最新的 UCITS Fund Portfolio Holdings CSV，生成一份**中文**深度投资日报。

# 硬性约束（必须严格遵守）
- 仅大中华区（Greater China）：境外持仓权重必须为 0%。
- 单只股票 ≤ 10% NAV。
- 5/40 规则：权重 > 5% 的持仓合计 ≤ 40%。
- A 股 < 30%；台湾 < 40%；现金 < 20%。
- 股票数量 23–27 只（超过 27 只时先移除一只再建议）。

# 分析三支柱
对每一只提及的股票，从以下三方面分析：Business Quality（业务质量）/ Valuation（估值）/ Operating Momentum（经营动能）。

# 输出格式（Markdown，仅正文，封面由系统自动添加，不要输出封面）
严格使用以下结构，**只输出 Markdown**，不要使用 ```代码块``` 包裹：

## Part 1 重大事件总览
逐封阅读邮件，不许跳过。同主题合并并标注「涉及邮件：X 封」。每条包含：
- 标题
- 时间 + 来源
- 链接状态（用纯文本 ✅ / ❌ 表示）
- 关键数据
- 分析（发生了什么 · 为何重要 · 对持仓影响）
- 重要性（🔴 高 / 🟡 中 / 📌 提示）
禁止泛宏观叙述。顺序：评级变动 → 宏观策略 → 行业专题 → 会议提醒。

## Part 2 投资想法总览
3–4 句话概括今日核心观点；若无机会写「今日无调仓建议」。

## Part 3 做多想法
对每只建议做多的标的，使用格式：
### [公司 / 代码]
- 当前价
- 投资逻辑（三支柱）
- 建仓价及理由
- 目标退出价及理由
- UCITS 权重（境外写 0%）

## Part 4 组合配置表
用 Markdown 表格，列：公司 | 当前价 | 建仓价 | 目标退出价 | 权重 | 合规检查。必须用数学验证上述所有约束（在「合规检查」列标注 OK / 违反项）。

## Part 5 链接读取汇总
用 Markdown 表格，列：时间 | 标题 | 链接数 | 读取结果。

# 排版要求
- 标题用 Markdown（`##` / `###`）。
- 列表用 `-`。
- 表格用 `| 列 | 列 |` 管道语法，第二行为分隔行 `|---|---|`。
- 重点词用 **粗体**。
- 链接状态用 ✅ / ❌ 纯文本；重要性用 🔴 / 🟡 / 📌。
- 语言：中文。"""

SYSTEM_PROMPT_EN = """You are an elite equity research analyst for Quam Asset Management, supporting the Lead PM in producing high-conviction long-term long ideas.

Based on today's fetched Outlook emails (already fetched; contents in the user message) and the latest UCITS Fund Portfolio Holdings CSV, generate an **English** in-depth investment daily report.

# Hard constraints (must be strictly observed)
- Greater China only: offshore position weight must be 0%.
- Single stock ≤ 10% NAV.
- 5/40 rule: holdings each > 5% must sum to ≤ 40%.
- A-shares < 30%; Taiwan < 40%; cash < 20%.
- 23–27 stocks (if over 27, remove one before suggesting).

# Three pillars
For each stock mentioned, analyze via: Business Quality / Valuation / Operating Momentum.

# Output format (Markdown body only — the cover is added automatically, do NOT output a cover)
Use exactly this structure, **output Markdown only**, do not wrap in ```code fences```:

## Part 1 Major Events Overview
Read every email, skip none. Merge same-topic items and label "涉及邮件：X 封" / "Emails: X". Each item:
- Title
- Time + Source
- Link status (plain text ✅ / ❌)
- Key data
- Analysis (what happened · why it matters · impact on holdings)
- Importance (🔴 high / 🟡 medium / 📌 note)
No generic macro narration. Order: rating changes → macro strategy → sector themes → meeting reminders.

## Part 2 Investment Idea Overview
3–4 sentences summarizing today's thesis; if no opportunity, write "今日无调仓建议" / "No repositioning suggested today".

## Part 3 Long Ideas
For each long idea:
### [Company / Ticker]
- Current price
- Investment thesis (three pillars)
- Entry price & rationale
- Target exit price & rationale
- UCITS weight (offshore = 0%)

## Part 4 Portfolio Allocation Table
Markdown table columns: Company | Current | Entry | Target Exit | Weight | Compliance. Mathematically verify all constraints above (mark OK / violation in Compliance).

## Part 5 Link Read Summary
Markdown table columns: Time | Title | # Links | Read Result.

# Formatting
- Headings use Markdown (## / ###).
- Lists use -.
- Tables use | col | col | pipe syntax with a --- separator row.
- Emphasize with **bold**.
- Link status as ✅ / ❌ plain text; importance 🔴 / 🟡 / 📌.
- Language: English."""


# ─────────────────────────────────────────────────────────────────────────────
# Payload assembly
# ─────────────────────────────────────────────────────────────────────────────
def _format_emails(emails_json: str) -> str:
    """Turn the fetched emails JSON into a readable, model-friendly block."""
    try:
        data = json.loads(Path(emails_json).read_text(encoding="utf-8"))
    except Exception as e:
        return f"(无法读取邮件 JSON: {e})"

    if not isinstance(data, list) or not data:
        return "(今日无抓取邮件)"

    parts = [f"【今日抓取邮件共 {len(data)} 封】\n"]
    for i, e in enumerate(data, 1):
        if not isinstance(e, dict):
            continue
        subj = e.get("subject", "(无主题)")
        sender = e.get("sender", "")
        t = e.get("time", "")
        body = (e.get("body") or "").strip()
        # Cap each body to keep the prompt bounded yet complete.
        if len(body) > 6000:
            body = body[:6000] + " …(截断)"
        parts.append(f"--- 邮件 {i} ---\n主题: {subj}\n发件人: {sender}\n时间: {t}\n正文:\n{body}\n")
    return "\n".join(parts)


def _read_holdings(holdings_csv: str | None) -> str:
    if not holdings_csv or not Path(holdings_csv).exists():
        return "(未提供持仓文件)"
    try:
        from rag.holdings_io import load_holdings_text
        text = load_holdings_text(holdings_csv)
        return text or "(持仓文件为空)"
    except Exception as e:
        try:
            return Path(holdings_csv).read_text(encoding="utf-8-sig")
        except Exception:
            return f"(无法读取持仓文件: {e})"


def _strip_fence(text: str) -> str:
    """Remove a leading/trailing ```markdown … ``` fence if the model added one."""
    t = text.strip()
    if t.startswith("```"):
        # drop first line (``` or ```markdown)
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
async def generate_daily_report(cfg: dict, emails_json: str,
                                 holdings_csv: str | None = None,
                                 language: str = "zh",
                                 date: str | None = None) -> str:
    """Generate the report **body** (Markdown) for the given inputs.

    Args:
        cfg:          LLM config dict with keys ``base_url``, ``api_key``, ``model``.
        emails_json:  path to the fetched ``today_emails.json``.
        holdings_csv: path to the latest UCITS holdings CSV (optional).
        language:     ``zh`` / ``en``.
        date:         ``YYYY-MM-DD`` (for context only; cover is added later).

    Returns the report Markdown (no cover). Raises on LLM / config errors.
    """
    from openai import AsyncOpenAI
    import httpx, certifi

    base_url = cfg.get("base_url") or "https://api.openai.com/v1"
    api_key = cfg.get("api_key") or "sk-placeholder"
    model = cfg.get("model") or "gpt-4o"

    system = SYSTEM_PROMPT_EN if language == "en" else SYSTEM_PROMPT_ZH
    date_line = f"报告日期：{date}\n\n" if date else ""
    user = (
        f"{date_line}"
        f"以下是今日抓取的全部邮件：\n\n{_format_emails(emails_json)}\n\n"
        f"以下是最新 UCITS Fund Portfolio Holdings CSV：\n\n{_read_holdings(holdings_csv)}\n\n"
        f"请严格按照系统指令的 Part 1–5 结构生成本日报（Markdown，仅正文）。"
    )

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    # NOTE: deepseek-v4-flash is a *reasoning* model — its internal reasoning
    # tokens count against max_tokens. With max_tokens=8000 the whole budget
    # was eaten by reasoning (reasoning_tokens=8000) and the visible report came
    # back EMPTY (finish_reason="length", content=""). We raise the budget so
    # reasoning + the actual report both fit, and refuse to emit a blank
    # document if the model still returns nothing.
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=24000,
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        fr = getattr(resp.choices[0], "finish_reason", None)
        raise RuntimeError(
            f"LLM 返回了空正文（finish_reason={fr}）。"
            f"可能是 reasoning 占满了 max_tokens 或内容被过滤，未生成可见报告。"
        )
    return _strip_fence(content)


def generate_daily_report_sync(cfg: dict, emails_json: str,
                               holdings_csv: str | None = None,
                               language: str = "zh",
                               date: str | None = None) -> str:
    """Blocking wrapper (uses asyncio.run). Convenience for non-async callers."""
    import asyncio
    return asyncio.run(
        generate_daily_report(cfg, emails_json, holdings_csv, language, date)
    )


if __name__ == "__main__":
    # Self-test without a real LLM: just verify payload assembly.
    import tempfile, os
    sample = [{"subject": "测试", "sender": "a@b.com", "time": "2026-07-13T08:00:00Z",
               "body": "腾讯维持买入，目标价 520。"}]
    p = Path(tempfile.gettempdir()) / "sample_emails.json"
    p.write_text(json.dumps(sample), encoding="utf-8")
    print(_format_emails(str(p)))
    print("--- holdings ---")
    print(_read_holdings(None))
