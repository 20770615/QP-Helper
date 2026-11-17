"""Cross-platform helpers for QP Helper (Windows / macOS / Linux)."""
from __future__ import annotations

import ctypes
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
        # Prefer real installed families. "SF Pro Text" is often unavailable as a
        # named CSS family and triggers Qt font-alias warnings on clean Macs.
        return '"Helvetica Neue", "PingFang SC", "Hiragino Sans GB", ".AppleSystemUIFont", sans-serif'
    if is_windows():
        return '"Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", sans-serif'
    return '"Noto Sans", "Noto Sans CJK SC", "DejaVu Sans", sans-serif'


def _objc_msgSend(objc, receiver, selector, *args):
    """Tiny objc_msgSend helper (ctypes) used by macOS lifecycle tweaks."""
    sel = objc.sel_registerName(selector if isinstance(selector, bytes) else selector.encode())
    objc.objc_msgSend.restype = ctypes.c_void_p
    if not args:
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return objc.objc_msgSend(receiver, sel)
    if len(args) == 1:
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        return objc.objc_msgSend(receiver, sel, args[0])
    raise TypeError("unsupported objc_msgSend arity")


def disable_macos_sudden_termination() -> None:
    """Keep the GUI alive: AppKit sudden/automatic termination can SIGKILL anon Python apps."""
    if not is_macos():
        return
    try:
        import ctypes
        import ctypes.util

        lib_name = ctypes.util.find_library("objc")
        if not lib_name:
            return
        objc = ctypes.cdll.LoadLibrary(lib_name)
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        NSProcessInfo = objc.objc_getClass(b"NSProcessInfo")
        info = _objc_msgSend(objc, NSProcessInfo, b"processInfo")
        if not info:
            return
        _objc_msgSend(objc, info, b"disableSuddenTermination")

        # disableAutomaticTermination: needs an NSString reason.
        NSString = objc.objc_getClass(b"NSString")
        reason = _objc_msgSend(
            objc,
            NSString,
            b"stringWithUTF8String:",
            ctypes.c_char_p(b"QP Helper desktop session"),
        )
        if reason:
            _objc_msgSend(objc, info, b"disableAutomaticTermination:", reason)
    except Exception:
        pass


def ensure_macos_activation_policy_regular() -> None:
    """Show a Dock icon and behave like a normal foreground app (not an accessory)."""
    if not is_macos():
        return
    try:
        import ctypes
        import ctypes.util

        lib_name = ctypes.util.find_library("objc")
        if not lib_name:
            return
        objc = ctypes.cdll.LoadLibrary(lib_name)
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        NSApplication = objc.objc_getClass(b"NSApplication")
        app = _objc_msgSend(objc, NSApplication, b"sharedApplication")
        if not app:
            return
        # NSApplicationActivationPolicyRegular == 0
        objc.objc_msgSend.restype = ctypes.c_bool
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        objc.objc_msgSend(
            app,
            objc.sel_registerName(b"setActivationPolicy:"),
            0,
        )
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _objc_msgSend(objc, app, b"activateIgnoringOtherApps:", ctypes.c_bool(True))
    except Exception:
        pass


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
    # Prefer a normal Dock app over an anonymous accessory process.
    ensure_macos_activation_policy_regular()
    # AppKit may otherwise sudden-terminate background Python GUIs (SIGKILL).
    disable_macos_sudden_termination()
    try:
        app.setQuitOnLastWindowClosed(True)
    except Exception:
        pass
