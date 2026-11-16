"""Markdown → DOCX renderer for the Quam UCITS daily report.

This module is the *document* half of the "Generate Report" skill. The LLM
harness produces the report **body** as Markdown; this module converts it to a
polished ``.docx`` and prepends the **locked** Quam cover page.

Why Markdown as the interchange format?
    The LLM already writes fluent Markdown (headings, bullets, tables). Rendering
    that to DOCX keeps the prompt simple and decouples "what to say" (harness)
    from "how it looks" (this module). The cover — which is contractually fixed
    and must never be altered by the model — is always forced here, not by the LLM.

Locked cover (DO NOT change — per .workbuddy/memory/MEMORY.md):
    3 blank lines
    GLOBAL EQUITY OPPORTUNITIES REPORT       (26pt Bold, #003D6B, centered)
    UCITS Portfolio Allocation               (16pt, centered)
    YYYY-MM-DD | Quam Asset Management       (12pt, centered)
    CONFIDENTIAL — For Internal Use Only     (red #CC0000, centered)
    page break
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor

# ── Palette ────────────────────────────────────────────────────────────────
NAVY = RGBColor(0x00, 0x3D, 0x6B)        # #003D6B — headings / cover title
RED = RGBColor(0xCC, 0x00, 0x00)         # #CC0000 — confidential line
BODY_FONT = "Microsoft YaHei"
BODY_SIZE = Pt(10.5)                       # 10–11pt body

# Locked filename pattern.
REPORT_FILENAME = "{date}_Global_Equity_Opportunities_Report_UCITS.docx"


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _set_cell_bg(cell, hex_color: str) -> None:
    """Shade a table-cell background (used for the header row)."""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _set_run_font(run, name: str = BODY_FONT, size=BODY_SIZE, bold=False, color=None, italic=False):
    run.font.name = name
    run.font.size = size
    run.font.bold = bold
    run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color
    # East-asian font binding so Microsoft YaHei actually applies to CJK.
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), name)
    rFonts.set(qn("w:hAnsi"), name)
    rFonts.set(qn("w:eastAsia"), name)


def _add_para(doc, text="", size=BODY_SIZE, bold=False, color=None, align=None,
              italic=False, space_after=6, space_before=0):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(space_before)
    if text:
        run = p.add_run(text)
        _set_run_font(run, size=size, bold=bold, color=color, italic=italic)
    return p


def _add_cover(doc, date_str: str) -> None:
    """Insert the locked Quam cover page, then a page break."""
    for _ in range(3):
        doc.add_paragraph()

    _add_para(doc, "GLOBAL EQUITY OPPORTUNITIES REPORT",
              size=Pt(26), bold=True, color=NAVY,
              align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    _add_para(doc, "UCITS Portfolio Allocation",
              size=Pt(16), color=NAVY,
              align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    _add_para(doc, f"{date_str} | Quam Asset Management",
              size=Pt(12), align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    _add_para(doc, "CONFIDENTIAL — For Internal Use Only",
              size=Pt(12), bold=True, color=RED,
              align=WD_ALIGN_PARAGRAPH.CENTER, space_after=6)

    # Page break after the cover.
    p = doc.add_paragraph()
    run = p.add_run()
    run.add_break(WD_BREAK.PAGE)


# ─────────────────────────────────────────────────────────────────────────────
# Inline formatting (**bold**, *italic*)
# ─────────────────────────────────────────────────────────────────────────────
def _append_inline(paragraph, text: str):
    """Parse **bold** and *italic* spans and append runs to ``paragraph``."""
    # Tokenize on **...** and *...*
    pattern = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            r = paragraph.add_run(text[pos:m.start()])
            _set_run_font(r)
        if m.group(1) is not None:
            r = paragraph.add_run(m.group(1))
            _set_run_font(r, bold=True)
        else:
            r = paragraph.add_run(m.group(2))
            _set_run_font(r, italic=True)
        pos = m.end()
    if pos < len(text):
        r = paragraph.add_run(text[pos:])
        _set_run_font(r)


# ─────────────────────────────────────────────────────────────────────────────
# Markdown block parser
# ─────────────────────────────────────────────────────────────────────────────
_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-\|]+\|?\s*$")


def _is_table_row(line: str) -> bool:
    return bool(_TABLE_RE.match(line))


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_sep_row(line: str) -> bool:
    cells = _split_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def render_markdown(doc, md: str) -> None:
    """Render a Markdown report body into ``doc`` (after the cover)."""
    lines = md.splitlines()
    i = 0
    n = len(lines)

    def _peek_table_block(start):
        # Collect consecutive table rows starting at `start`.
        rows = []
        j = start
        while j < n and _is_table_row(lines[j]):
            rows.append(lines[j])
            j += 1
        return rows, j

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Skip blank lines (they are paragraph separators, handled by spacing).
        if not stripped:
            i += 1
            continue

        # Horizontal rule → a thin spacer (no visible line, keeps it clean).
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            _add_para(doc, "", space_after=4)
            i += 1
            continue

        # Table block.
        if _is_table_row(line):
            rows, i = _peek_table_block(i)
            if len(rows) >= 2 and _is_sep_row(rows[1]):
                _emit_table(doc, rows)
            else:
                # Not a valid table (missing separator) — dump as text.
                for r in rows:
                    p = doc.add_paragraph()
                    _append_inline(p, r)
                i += 0  # already advanced by _peek_table_block
            continue

        # Headings.
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            _emit_heading(doc, level, text)
            i += 1
            continue

        # Numbered list.
        if re.match(r"^\d+[.)]\s+", stripped):
            i = _emit_list(doc, lines, i, ordered=True)
            continue

        # Bullet list.
        if re.match(r"^[-*]\s+", stripped):
            i = _emit_list(doc, lines, i, ordered=False)
            continue

        # Plain paragraph.
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        _append_inline(p, stripped)
        i += 1


def _emit_heading(doc, level: int, text: str):
    sizes = {1: Pt(18), 2: Pt(14), 3: Pt(12), 4: Pt(11)}
    size = sizes.get(level, Pt(11))
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10 if level <= 2 else 6)
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run(text)
    _set_run_font(run, size=size, bold=True, color=NAVY)
    # Bottom border under level-1/2 headings for a report feel.
    if level <= 2:
        pPr = p._p.get_or_add_pPr()
        pbdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "2")
        bottom.set(qn("w:color"), "003D6B")
        pbdr.append(bottom)
        pPr.append(pbdr)


def _emit_table(doc, rows: list[str]):
    header = _split_row(rows[0])
    data = [_split_row(r) for r in rows[2:]]  # skip header + separator
    cols = max(len(header), *(len(d) for d in data)) if data else len(header)

    table = doc.add_table(rows=1, cols=cols)
    table.style = "Table Grid"
    table.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Header row.
    hdr_cells = table.rows[0].cells
    for c in range(cols):
        txt = header[c] if c < len(header) else ""
        cell = hdr_cells[c]
        cell.text = ""
        p = cell.paragraphs[0]
        run = p.add_run(txt)
        _set_run_font(run, size=Pt(10), bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
        _set_cell_bg(cell, "003D6B")

    # Data rows.
    for d in data:
        cells = table.add_row().cells
        for c in range(cols):
            txt = d[c] if c < len(d) else ""
            cell = cells[c]
            cell.text = ""
            p = cell.paragraphs[0]
            _append_inline(p, txt)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _emit_list(doc, lines, start, ordered: bool):
    """Emit a list (single level + indented continuations) starting at ``start``.

    Returns the index just past the list block so the caller can continue.
    """
    idx = start
    n = len(lines)
    last_p = None
    while idx < n:
        raw = lines[idx]
        stripped = raw.strip()
        m = re.match(r"^(\d+[.)]|[-*])\s+(.*)$", stripped)
        if m:
            text = m.group(2).strip()
            p = doc.add_paragraph(style="List Bullet" if not ordered else "List Number")
            p.paragraph_format.space_after = Pt(2)
            _append_inline(p, text)
            last_p = p
            idx += 1
        elif raw.startswith(" ") and stripped and last_p is not None:
            # Indented continuation → append to the previous list item.
            run = last_p.add_run(" " + stripped)
            _set_run_font(run)
            idx += 1
        else:
            break
    return idx


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def build_report(markdown_body: str, out_path: str | Path, date_str: str,
                 language: str = "zh") -> Path:
    """Render a full report (.docx) from a Markdown body + cover.

    Args:
        markdown_body: report body (Parts 1–5), Markdown. Cover is forced.
        out_path:       directory OR full file path. If a directory, the locked
                        filename ``YYYYMMDD_Global_Equity_Opportunities_Report_UCITS.docx``
                        is used.
        date_str:       ``YYYY-MM-DD`` for the cover + filename.
        language:       ``zh`` / ``en`` (reserved for future localization).

    Returns the written Path.
    """
    out_path = Path(out_path)
    # Resolve directory-vs-file: an explicit .docx path is used as-is; anything
    # else (existing dir, or a non-existent path without a suffix) is treated
    # as a directory and given the locked filename.
    if out_path.suffix.lower() == ".docx":
        pass
    else:
        out_path = out_path / REPORT_FILENAME.format(date=date_str.replace("-", ""))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    # Default body font / size.
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = BODY_SIZE
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

    _add_cover(doc, date_str)
    render_markdown(doc, markdown_body)
    doc.save(str(out_path))
    return out_path


if __name__ == "__main__":
    # Tiny self-test: render a sample and print the resulting structure.
    sample = """## Part 1 重大事件总览

### 评级变动
- **标题**：腾讯控股维持买入
- 时间+来源：2026-07-13 | 大摩
- 链接状态：✅
- 关键数据：目标价 520 HKD
- 分析：估值修复空间大
- 重要性：🔴

## Part 4 组合配置表

| 公司 | 当前价 | 建仓价 | 目标退出价 | 权重 | 合规检查 |
|------|--------|--------|------------|------|----------|
| 腾讯 | 480 | 450 | 520 | 8% | OK |
"""
    p = build_report(sample, Path("."), "2026-07-13")
    print("wrote", p, p.stat().st_size, "bytes")
