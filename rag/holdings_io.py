"""Load UCITS / portfolio holdings from common tabular file formats.

Settings historically stored a CSV path (``holdings_csv_path``). The path may
now point at CSV / TSV / TXT / Excel / JSON; we always return plain text that
the agent / report prompt can read.
"""

from __future__ import annotations

from pathlib import Path

HOLDINGS_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".xlsm", ".json"}

# Preferred default filenames under the shared root (first hit wins).
DEFAULT_HOLDINGS_NAMES = (
    "ucits_fund_holdings.csv",
    "ucits_fund_holdings.xlsx",
    "ucits_fund_holdings.xls",
    "ucits_fund_holdings.xlsm",
    "ucits_fund_holdings.tsv",
    "ucits_fund_holdings.json",
    "holdings.csv",
    "holdings.xlsx",
    "positions.csv",
    "positions.xlsx",
)


def find_default_holdings(root: Path | str | None) -> Path | None:
    """Return the first existing default holdings file under ``root``."""
    if not root:
        return None
    base = Path(root)
    if not base.is_dir():
        return None
    for name in DEFAULT_HOLDINGS_NAMES:
        p = base / name
        if p.is_file():
            return p
    return None


def load_holdings_text(path: str | Path | None, max_chars: int = 200_000) -> str:
    """Load a holdings file as UTF-8 text for LLM / report injection.

    Returns an empty string if the path is missing or unreadable.
    """
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""

    suffix = p.suffix.lower()
    try:
        if suffix in {".csv", ".tsv", ".txt"}:
            text = p.read_text(encoding="utf-8-sig")
        elif suffix in {".xlsx", ".xls", ".xlsm"}:
            text = _excel_to_text(p)
        elif suffix == ".json":
            text = _json_to_text(p)
        else:
            # Unknown but user picked it — try UTF-8 text as a last resort.
            text = p.read_text(encoding="utf-8-sig")
    except Exception as e:
        return f"(无法读取持仓文件 {p.name}: {e})"

    text = (text or "").strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(truncated)"
    return text


def _excel_to_text(path: Path) -> str:
    import pandas as pd

    # Read all sheets; if only one, emit a plain CSV table. Multi-sheet workbooks
    # get a sheet header so the agent can still see everything.
    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    if not sheets:
        return ""
    if len(sheets) == 1:
        df = next(iter(sheets.values()))
        return df.fillna("").to_csv(index=False)
    parts: list[str] = []
    for name, df in sheets.items():
        parts.append(f"### Sheet: {name}")
        parts.append(df.fillna("").to_csv(index=False).rstrip())
    return "\n\n".join(parts)


def _json_to_text(path: Path) -> str:
    import json

    import pandas as pd

    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, list):
        if not raw:
            return ""
        if all(isinstance(x, dict) for x in raw):
            return pd.DataFrame(raw).fillna("").to_csv(index=False)
        return json.dumps(raw, ensure_ascii=False, indent=2)
    if isinstance(raw, dict):
        for key in ("holdings", "positions", "portfolio", "data", "rows"):
            val = raw.get(key)
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                return pd.DataFrame(val).fillna("").to_csv(index=False)
        return json.dumps(raw, ensure_ascii=False, indent=2)
    return str(raw)
