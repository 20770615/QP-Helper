"""Cross-platform helpers for QP Helper (Windows / macOS / Linux)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def default_shared_root() -> str:
    """Platform-appropriate default shared KB root (empty until user picks)."""
    home = Path.home()
    if is_macos():
        return str(home / "Documents" / "QP Helper" / "Shared")
    if is_windows():
        # Keep empty so Windows teams still configure G: / network share explicitly.
        return ""
    return str(home / "Documents" / "QP-Helper" / "Shared")


def default_g_paths() -> dict[str, str]:
    root = default_shared_root()
    if not root:
        return {"email_summary": "", "meetings": "", "docs": ""}
    base = Path(root)
    return {
        "email_summary": str(base / "email_data") + os.sep,
        "meetings": str(base / "meetings") + os.sep,
        "docs": str(base / "docs") + os.sep,
    }


def ui_font_families() -> str:
    """CSS font-family stack that works on macOS, Windows, and Linux."""
    if is_macos():
        return '"SF Pro Text", "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", sans-serif'
    if is_windows():
        return '"Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", sans-serif'
    return '"Noto Sans", "Noto Sans CJK SC", "DejaVu Sans", sans-serif'


def venv_python_candidates(root: Path) -> list[Path]:
    """Return likely interpreter paths under a distribution / portable root."""
    root = Path(root)
    names: list[str]
    if is_windows():
        names = ["pythonw.exe", "python.exe"]
        rels = [
            Path(".venv") / "Scripts",
            Path("Scripts"),
            Path("_internal") / ".venv" / "Scripts",
        ]
    else:
        names = ["python3", "python"]
        rels = [
            Path(".venv") / "bin",
            Path("bin"),
            Path("_internal") / ".venv" / "bin",
        ]
    out: list[Path] = []
    for rel in rels:
        for name in names:
            out.append(root / rel / name)
    return out


def find_venv_python(roots: list[Path], *, skip_self: bool = True) -> Path | None:
    """Find a usable venv interpreter across candidate roots."""
    self_exe = Path(sys.executable).resolve() if skip_self else None
    for root in roots:
        for cand in venv_python_candidates(root):
            try:
                if not cand.exists():
                    continue
                resolved = cand.resolve()
                if self_exe is not None and resolved == self_exe:
                    continue
                return resolved
            except Exception:
                continue
    return None


def subprocess_hidden_kwargs() -> dict:
    """Extra Popen kwargs to hide console windows on Windows only."""
    if is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def open_path(path: str | Path) -> None:
    """Open a file or directory with the OS default handler."""
    p = str(path)
    if is_windows():
        os.startfile(p)  # type: ignore[attr-defined]
        return
    if is_macos():
        subprocess.Popen(["open", p])
        return
    subprocess.Popen(["xdg-open", p])


def reveal_in_file_manager(path: str | Path) -> None:
    """Reveal a file in Explorer / Finder / file manager."""
    p = Path(path).resolve()
    if is_windows():
        subprocess.Popen(["explorer", "/select,", str(p)])
        return
    if is_macos():
        subprocess.Popen(["open", "-R", str(p)])
        return
    # Linux: open the parent directory
    subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])


def browser_channel_preference() -> list[str]:
    """Preferred Playwright / Chromium channel order for Outlook login."""
    if is_macos():
        return ["chrome", "msedge", "chromium"]
    if is_windows():
        return ["msedge", "chrome", "chromium"]
    return ["chromium", "chrome", "msedge"]


def apply_qt_macos_tweaks(app) -> None:
    """macOS-specific Qt application identity / chrome tweaks."""
    if not is_macos():
        return
    app.setApplicationName("QP Helper")
    app.setOrganizationName("Quam Plus")
    app.setOrganizationDomain("quamplus.local")
    # Ensure menus merge into the macOS system menu bar.
    try:
        app.setAttribute(app.ApplicationAttribute.AA_DontShowIconsInMenus, True)
    except Exception:
        pass
