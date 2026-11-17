"""QP Helper — recovered / reconstructed UI entry (from QP_Helper.exe bytecode).

This is NOT a perfect 1:1 decompilation of every line. It rebuilds a maintainable
PyQt6 app using:
  - extracted class/method inventories from qp_email_helper.pyc
  - recovered i18n / config / rag_client / shared_store modules
  - the existing _internal/rag_service backend

Run (from repo root)::

    # macOS / Linux
    .venv/bin/python src/qp_email_helper.py
    # Windows
    .venv\\Scripts\\python.exe src\\qp_email_helper.py
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# Make src / recovered_src importable when launched from portable root or this folder.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_ROOT = (
    _HERE.parent
    if _HERE.name in {"recovered_src", "recovered_src_ui_beauty", "src"}
    else _HERE
)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PyQt6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QColor, QKeySequence, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QButtonGroup,
)
import shared_store

from platform_compat import (
    apply_qt_macos_tweaks,
    find_venv_python,
    is_macos,
    subprocess_hidden_kwargs,
    ui_font_families,
)

import config_manager
from config_manager import (
    get_display_name,
    set_display_name,
    get_language,
    set_language,
    get_llm_config,
    load_app_config,
    update_app_config,
    get_rag_service_port,
    get_actor,
    get_shared_root,
    set_shared_root,
    get_holdings_csv_path,
    set_holdings_csv_path,
    normalize_llm_model,
)
from i18n import t, placeholder_skills
from rag_client import RagClient
from popup_manager import PopupManager
from audit_logger import log_action
from views_database import DatabaseView
from views_prompts import PromptMgmtView
from views_email import EmailFetchView
from views_upload import UploadHistoryView


def _load_stylesheet() -> str:
    """Prefer bytecode-extracted original QSS; fall back to rebuilt theme."""
    qss_path = _HERE / "styles.qss"
    if qss_path.exists():
        try:
            raw = qss_path.read_text(encoding="utf-8")
            # Rewrite the shared font stack for the active platform.
            for needle in (
                '"Helvetica Neue", "PingFang SC", "Hiragino Sans GB", ".AppleSystemUIFont", "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif',
                '"SF Pro Text", "Helvetica Neue", "PingFang SC", "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif',
                '"Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", sans-serif',
            ):
                if needle in raw:
                    return raw.replace(needle, ui_font_families())
            return raw
        except Exception:
            pass
    return _FALLBACK_STYLE


# ── Fallback style tokens (if styles.qss missing) ─────────────────────────────
_FALLBACK_STYLE = """
QMainWindow, QWidget#central {
  background: #0f1115;
  color: #e8eaed;
  font-family: __UI_FONTS__;
  font-size: 13px;
}
QWidget#sidebar {
  background: #151821;
  border-right: 1px solid #252a36;
}
QPushButton.nav {
  text-align: left;
  padding: 10px 14px;
  border: none;
  border-radius: 10px;
  color: #aeb4c0;
  background: transparent;
}
QPushButton.nav:hover { background: #1d2330; color: #fff; }
QPushButton.nav[active="true"] {
  background: #2a2418;
  color: #f0c674;
  border: 1px solid #3d3420;
}
QLabel#pageTitle { font-size: 22px; font-weight: 700; color: #f5f6f8; }
QLabel#pageSub { color: #8b93a7; }
QLabel#svcOnline { color: #3ecf8e; }
QLabel#svcOffline { color: #ff6b6b; }
QTextEdit#chatInput, QLineEdit {
  background: #1a1f2b;
  border: 1px solid #2c3344;
  border-radius: 12px;
  padding: 10px 12px;
  color: #e8eaed;
}
QPushButton#sendBtn {
  background: #c9a227;
  color: #1a1408;
  border: none;
  border-radius: 12px;
  padding: 10px 18px;
  font-weight: 700;
}
QPushButton#sendBtn:disabled { background: #4a4330; color: #888; }
""".replace("__UI_FONTS__", ui_font_families())

class OnboardingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("onboard.title"))
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(t("onboard.title")))
        lay.addWidget(QLabel(t("onboard.sub")))
        lay.addWidget(QLabel(t("onboard.name_label")))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(t("onboard.name_ph"))
        lay.addWidget(self.name_edit)
        lay.addWidget(QLabel(t("onboard.lang_label")))
        row = QHBoxLayout()
        self.lang_en = QRadioButton(t("onboard.lang_en"))
        self.lang_zh = QRadioButton(t("onboard.lang_zh"))
        (self.lang_zh if get_language() == "zh" else self.lang_en).setChecked(True)
        row.addWidget(self.lang_en)
        row.addWidget(self.lang_zh)
        lay.addLayout(row)
        lay.addWidget(QLabel(t("onboard.note")))
        btn = QPushButton(t("onboard.enter"))
        btn.clicked.connect(self._enter)
        lay.addWidget(btn)

    def _enter(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, t("common.warning") if "common.warning" in {} else "Warning", t("onboard.empty_name"))
            return
        set_display_name(name)
        set_language("zh" if self.lang_zh.isChecked() else "en")
        self.accept()


class ServiceProbe(QThread):
    result = pyqtSignal(bool)

    def __init__(self, base_url: str, push_cfg: dict | None = None):
        super().__init__()
        self.base_url = base_url
        self.push_cfg = push_cfg or {}

    def run(self):
        client = RagClient(self.base_url, timeout=3.0)
        alive = client.is_alive(timeout=2.0)
        if alive and self.push_cfg:
            try:
                client.update_config(**self.push_cfg)
            except Exception:
                pass
        self.result.emit(alive)


class AskWorker(QThread):
    finished_ok = pyqtSignal(dict)
    finished_err = pyqtSignal(str)

    def __init__(self, client: RagClient, query: str, history: list, force_retrieve=False, filter_paths=None):
        super().__init__()
        self.client = client
        self.query = query
        self.history = history
        self.force_retrieve = force_retrieve
        self.filter_paths = filter_paths or []

    def run(self):
        try:
            resp = self.client.ask(
                self.query,
                history=self.history,
                force_retrieve=self.force_retrieve,
                filter_paths=self.filter_paths,
            )
            self.finished_ok.emit(resp)
        except Exception as e:
            self.finished_err.emit(str(e))


class RagServiceManager:
    """Start/stop local rag_service using bundled .venv python."""

    def __init__(self, port: int | None = None):
        self.port = port or get_rag_service_port()
        self._proc = None

    def _find_service_script(self) -> Path | None:
        candidates = [
            _ROOT / "_internal" / "rag_service" / "main.py",
            _ROOT / "rag_service" / "main.py",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _find_python(self) -> Path | None:
        found = find_venv_python([_ROOT, _HERE], skip_self=False)
        if found:
            return found
        # Dev fallback: current interpreter (works on macOS/Linux/Windows).
        return Path(sys.executable)

    @staticmethod
    def _python_is_310_plus(py: Path) -> bool:
        try:
            import subprocess

            r = subprocess.run(
                [str(py), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
                capture_output=True,
                timeout=10,
                **subprocess_hidden_kwargs(),
            )
            return r.returncode == 0
        except Exception:
            return False

    def start(self) -> tuple[bool, str]:
        if self.is_running():
            return True, "already running"
        script = self._find_service_script()
        py = self._find_python()
        if not script:
            return False, (
                "rag_service/main.py not found — run setup / pull the Mac branch "
                "that includes the rag/ + rag_service/ packages"
            )
        if not py:
            return False, t("st.service_error_no_python")
        if not self._python_is_310_plus(py):
            return False, (
                f"{py} is Python <3.10 — recreate .venv with ./scripts/setup_macos.sh "
                "(Apple CLT python3 is 3.9.x and breaks LanceDB)"
            )
        import subprocess

        env = os.environ.copy()
        llm = get_llm_config()
        if llm.get("base_url"):
            env["RAG_LLM_BASE_URL"] = llm["base_url"]
        if llm.get("api_key"):
            env["RAG_LLM_API_KEY"] = llm["api_key"]
        if llm.get("model"):
            env["RAG_LLM_MODEL"] = llm["model"]
        env["RAG_CONFIG_PATH"] = str(config_manager.APP_CONFIG_FILE)
        # Default to LanceDB on all platforms (matches scripts/run_qdrant.*).
        env.setdefault("RAG_VECTOR_BACKEND", "lancedb")
        log_dir = _ROOT / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "rag_service.log"
            log_fh = open(log_path, "a", encoding="utf-8")
        except Exception:
            log_fh = subprocess.DEVNULL
            log_path = None
        try:
            self._proc = subprocess.Popen(
                [str(py), str(script), "--host", "127.0.0.1", "--port", str(self.port)],
                cwd=str(script.parent.parent),
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                **subprocess_hidden_kwargs(),
            )
            msg = f"started pid={self._proc.pid}"
            if log_path is not None:
                msg += f" log={log_path}"
            return True, msg
        except Exception as e:
            return False, str(e)

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

    def is_running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)


class HomeView(QWidget):
    """Recovered Home / chat page (core ask flow)."""

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.history: list[dict] = []
        self._worker: AskWorker | None = None
        self._ask_source_paths: list[str] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 24, 36, 20)
        root.setSpacing(14)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        self.page_title = QLabel(t("header.home"))
        self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel(t("header.home.sub"))
        self.page_sub.setObjectName("pageSub")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_sub)
        head.addLayout(titles, 1)
        self.svc_label = QLabel(t("svc.checking"))
        self.svc_label.setObjectName("svcOffline")
        head.addWidget(self.svc_label, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        self.skill_btn = QPushButton(t("chat.skills"))
        self.skill_btn.setFixedWidth(90)
        root.addWidget(self.skill_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_host = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_host)
        self.chat_layout.setSpacing(12)
        self.chat_layout.addStretch(1)
        self.scroll.setWidget(self.chat_host)
        root.addWidget(self.scroll, 1)

        self.welcome = QLabel(t("chat.welcome", name=get_actor()))
        self.welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome.setStyleSheet("color:#8b93a7;font-size:16px;padding:40px;")
        self.chat_layout.insertWidget(0, self.welcome)

        bottom = QHBoxLayout()
        self.input = QTextEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText(t("chat.input_ph"))
        self.input.setFixedHeight(72)
        self.input.setAcceptRichText(False)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Scope chip shown when user picks @ → Ask about folder/file.
        self._scope_chip = QPushButton()
        self._scope_chip.setObjectName("scopeChip")
        self._scope_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scope_chip.setStyleSheet(
            "QPushButton#scopeChip{background:#e0f2fe;color:#0369a1;border:1px solid #bae6fd;"
            "border-radius:10px;padding:2px 10px;font-size:12px;text-align:left;}"
            "QPushButton#scopeChip:hover{background:#bae6fd;}"
        )
        self._scope_chip.setVisible(False)
        self._scope_chip.clicked.connect(self._clear_ask_source)

        input_wrap = QWidget()
        input_v = QVBoxLayout(input_wrap)
        input_v.setContentsMargins(0, 0, 0, 0)
        input_v.setSpacing(4)
        input_v.addWidget(self._scope_chip)
        input_v.addWidget(self.input)

        self.send_btn = QPushButton(t("chat.send"))
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.clicked.connect(self._send)
        bottom.addWidget(input_wrap, 1)
        bottom.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(bottom)

        # @ mention menu (restored from Windows bytecode; missing after Mac recovery).
        self._at_menu = QMenu(self)
        self._at_menu.setObjectName("atMenu")
        self._at_menu.setStyleSheet(
            "QMenu#atMenu{background:#fff;border:1px solid #cbd5e1;border-radius:12px;"
            "padding:4px;outline:0;}"
            "QMenu#atMenu::item{height:34px;padding:0 12px;border-radius:8px;color:#1a1a1a;}"
            "QMenu#atMenu::item:selected{background:#e0f2fe;color:#0369a1;}"
            "QMenu#atMenu::item:disabled{color:#999;}"
        )
        self._at_menu.hide()
        self._at_menu.triggered.connect(self._on_at_triggered)
        self._at_index = 0
        self._at_menu_pending = False
        self.input.textChanged.connect(self._on_input_changed)
        self.input.installEventFilter(self)

    def retranslate(self):
        self.page_title.setText(t("header.home"))
        self.page_sub.setText(t("header.home.sub"))
        self.skill_btn.setText(t("chat.skills"))
        self.input.setPlaceholderText(t("chat.input_ph"))
        self.send_btn.setText(t("chat.send"))
        self.welcome.setText(t("chat.welcome", name=get_actor()))

    def set_input(self, text: str):
        """Fill chat input (used by PromptMgmtView 'Use in chat')."""
        self.input.setPlainText(text or "")
        self.input.setFocus()

    def set_service_alive(self, alive: bool):
        if alive:
            self.svc_label.setText(t("svc.online"))
            self.svc_label.setObjectName("svcOnline")
        else:
            self.svc_label.setText(t("svc.offline"))
            self.svc_label.setObjectName("svcOffline")
        self.svc_label.style().unpolish(self.svc_label)
        self.svc_label.style().polish(self.svc_label)

    def _hide_welcome(self):
        self.welcome.hide()

    def _add_bubble(self, text: str, kind: str = "user"):
        """Append a chat turn. User = right bubble; assistant = left plain text."""
        self._hide_welcome()
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 2, 0, 2)
        row_lay.setSpacing(8)

        body = QFrame()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(12, 10, 12, 8)
        body_lay.setSpacing(6)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        # Escape is not applied — callers may pass light HTML (citations).
        html = text if "<" in text and ">" in text else text.replace("\n", "<br>")
        browser.setHtml(html)
        browser.setFrameShape(QFrame.Shape.NoFrame)
        browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        def _fit():
            browser.document().setTextWidth(max(120, browser.viewport().width()))
            h = int(browser.document().size().height()) + 8
            browser.setFixedHeight(max(28, min(h, 900)))

        browser.document().contentsChanged.connect(_fit)
        QTimer.singleShot(0, _fit)

        if kind == "user":
            body.setObjectName("chatBubbleUser")
            browser.setObjectName("chatBubbleUserText")
            browser.setStyleSheet("background:transparent;color:#0f172a;")
            body_lay.setContentsMargins(14, 10, 14, 10)
            body_lay.addWidget(browser)
            # Cursor-style: user bubble on the right, capped width.
            body.setMaximumWidth(520)
            row_lay.addStretch(1)
            row_lay.addWidget(body, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        elif kind == "agent":
            # Assistant: left-aligned prose — no twin dark card.
            body.setObjectName("chatBubbleAgent")
            body.setStyleSheet(
                "QFrame#chatBubbleAgent{background:transparent;border:none;border-radius:0;}"
            )
            browser.setObjectName("chatBubbleAgentText")
            browser.setStyleSheet(
                "background:transparent;color:#111827;font-size:13px;padding:0;"
            )
            body_lay.setContentsMargins(4, 2, 4, 2)
            body_lay.addWidget(browser)
            copy_btn = QPushButton(t("chat.copy"))
            copy_btn.setFlat(True)
            copy_btn.setStyleSheet(
                "QPushButton{color:#6b7280;border:none;padding:0 4px;font-size:11px;}"
                "QPushButton:hover{color:#b45309;}"
            )
            copy_btn.clicked.connect(
                lambda: QApplication.clipboard().setText(browser.toPlainText())
            )
            body_lay.addWidget(copy_btn, 0, Qt.AlignmentFlag.AlignLeft)
            row_lay.addWidget(body, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            row_lay.addStretch(1)
        else:
            body.setObjectName("chatBubbleInfo")
            browser.setObjectName("chatBubbleAgentText")
            browser.setStyleSheet("background:transparent;color:#1e3a5f;")
            body_lay.addWidget(browser)
            body.setMaximumWidth(640)
            row_lay.addWidget(body, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            row_lay.addStretch(1)

        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        QTimer.singleShot(
            50,
            lambda: self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().maximum()
            ),
        )

    def _on_input_changed(self):
        """Open the @ command menu when the input starts with a bare `@` token."""
        text = self.input.toPlainText()
        # Bare `@` (or `@partial` without a space) → show / filter the menu.
        if text.startswith("@") and " " not in text[1:]:
            self._show_at_menu(text[1:].strip())
        else:
            self._hide_at_menu()

    def _show_at_menu(self, filter_text: str = ""):
        """Populate and show the @ menu above the chat input.

        Uses non-blocking ``popup()`` (deferred) instead of modal ``exec()``.
        Modal exec nested inside ``textChanged`` / IME composition is unreliable
        on macOS and can swallow the @ keystroke or steal focus permanently.
        """
        try:
            self._at_menu.clear()
            items = (
                ("at.menu_search", "search"),
                ("at.menu_summary", "summary"),
                ("at.menu_ask_folder", "ask_folder"),
                ("at.menu_ask_file", "ask_file"),
            )
            for key, cmd in items:
                label = t(key)
                if filter_text and filter_text.lower() not in label.lower():
                    continue
                action = self._at_menu.addAction(label)
                action.setData(cmd)
            if self._at_menu.isEmpty():
                self._hide_at_menu()
                return
            self._at_index = 0
            acts = [a for a in self._at_menu.actions() if a.isEnabled()]
            if acts:
                self._at_menu.setActiveAction(acts[0])

            # Already open → just refresh actions (lets user type to filter).
            if self._at_menu.isVisible():
                return

            def _popup():
                self._at_menu_pending = False
                if self._at_menu.isEmpty():
                    return
                # Re-check: user may have typed a space / deleted @ meanwhile.
                cur = self.input.toPlainText()
                if not (cur.startswith("@") and " " not in cur[1:]):
                    return
                gp = self.input.mapToGlobal(QPoint(0, 0))
                h = self._at_menu.sizeHint().height()
                y = max(8, gp.y() - h - 4)
                self._at_menu.popup(QPoint(gp.x(), y))

            # Defer so we are outside the textChanged / inputMethod event stack
            # (critical on macOS where modal exec nested in IME breaks @).
            if not self._at_menu_pending:
                self._at_menu_pending = True
                QTimer.singleShot(0, _popup)
        except Exception as e:
            QMessageBox.critical(self, "AT menu error", f"{type(e).__name__}: {e}")

    def _hide_at_menu(self):
        if self._at_menu.isVisible():
            self._at_menu.hide()

    def eventFilter(self, obj, event):
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            # macOS / IME: also open the menu when the physical @ character arrives.
            # Qt.Key.Key_At == 64; some layouts deliver '@' via text() only.
            text = event.text() or ""
            if (key == Qt.Key.Key_At or text == "@") and not self._at_menu.isVisible():
                # Let the character insert first; textChanged will open the menu.
                # Returning False keeps default insert behavior.
                pass
            if self._at_menu.isVisible():
                acts = [a for a in self._at_menu.actions() if a.isEnabled()]
                if acts:
                    if key == Qt.Key.Key_Down:
                        self._at_index = min(self._at_index + 1, len(acts) - 1)
                        self._at_menu.setActiveAction(acts[self._at_index])
                        return True
                    if key == Qt.Key.Key_Up:
                        self._at_index = max(self._at_index - 1, 0)
                        self._at_menu.setActiveAction(acts[self._at_index])
                        return True
                    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                        self._on_at_triggered(acts[self._at_index])
                        return True
                if key == Qt.Key.Key_Escape:
                    self._hide_at_menu()
                    return True
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                modifiers = event.modifiers()
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    return False  # Shift+Enter → newline
                self._send()
                return True
        return super().eventFilter(obj, event)

    def _on_at_triggered(self, action):
        if action is None:
            return
        data = action.data()
        if not data:
            return
        self._hide_at_menu()
        if data == "search":
            self.input.setPlainText("@" + t("at.cmd_search") + " ")
            self.input.setFocus()
            cursor = self.input.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.input.setTextCursor(cursor)
            return
        if data == "summary":
            # Full email-summary panel is not yet restored; keep a clear tip.
            QMessageBox.information(
                self,
                t("common.info"),
                t("at.menu_summary") + " — " + t("at.scope_hint"),
            )
            self.input.clear()
            return
        if data == "ask_folder":
            try:
                path = QFileDialog.getExistingDirectory(
                    self,
                    t("at.pick_folder_title"),
                    str(shared_store.kb_root()),
                )
            except Exception as e:
                QMessageBox.critical(self, t("common.error"), f"folder dialog: {e}")
                return
            if path:
                self._set_ask_source(path)
            return
        if data == "ask_file":
            try:
                path, _ = QFileDialog.getOpenFileName(
                    self,
                    t("at.pick_file_title"),
                    str(shared_store.kb_root()),
                )
            except Exception as e:
                QMessageBox.critical(self, t("common.error"), f"file dialog: {e}")
                return
            if path:
                self._set_ask_source(path)

    def _set_ask_source(self, path: str):
        norm = os.path.normpath(path).replace("\\", "/")
        self._ask_source_paths = [norm]
        base = os.path.basename(path.rstrip("/\\")) or path
        self._scope_chip.setText(t("at.scope_chip").format(name=base))
        self._scope_chip.setVisible(True)
        self.input.clear()
        self.input.setFocus()

    def _clear_ask_source(self):
        self._ask_source_paths = []
        self._scope_chip.setVisible(False)

    def _send(self):
        raw = (
            self.input.toPlainText()
            .strip()
            .replace("\u2029", "\n")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        if not raw:
            return

        force_retrieve = False
        filter_paths: list[str] = []
        query = raw

        if self._ask_source_paths:
            q = raw
            if q.startswith("@source"):
                q = q[len("@source") :].strip()
            if not q:
                return
            query = q
            filter_paths = list(self._ask_source_paths)
            self._ask_source_paths = []
            self._scope_chip.setVisible(False)
            force_retrieve = True
        elif raw.startswith("@source"):
            query = raw[len("@source") :].strip()
            if query:
                QMessageBox.information(self, t("common.info"), t("at.scope_hint"))
        else:
            search_cmds = ("@" + t("at.cmd_search"), "@检索文件", "@Search files")
            matched = False
            for prefix in search_cmds:
                if raw.startswith(prefix):
                    query = raw[len(prefix) :].strip()
                    force_retrieve = True
                    matched = True
                    break
            if matched and not query:
                QMessageBox.information(
                    self,
                    t("common.info"),
                    t("at.search_empty", cmd=t("at.cmd_search")),
                )
                return

        if not self.main.rag_client or not self.main.rag_client.is_alive():
            self._add_bubble(t("chat.offline"), "info")
            return
        self._hide_at_menu()
        self._add_bubble(query, "user")
        self.history.append({"role": "user", "content": query})
        self.input.clear()
        self.send_btn.setEnabled(False)
        self.send_btn.setText(t("chat.thinking"))
        self._worker = AskWorker(
            self.main.rag_client,
            query,
            self.history[:-1],
            force_retrieve=force_retrieve,
            filter_paths=filter_paths,
        )
        self._worker.finished_ok.connect(self._on_answer)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()
        log_action("ask", query=query)

    def _on_answer(self, resp: dict):
        self.send_btn.setEnabled(True)
        self.send_btn.setText(t("chat.send"))
        answer = (resp or {}).get("answer") or ""
        self.history.append({"role": "assistant", "content": answer})
        self._add_bubble(answer, "agent")
        cites = (resp or {}).get("citations") or []
        if cites:
            bits = []
            for c in cites[:8]:
                src = c.get("source") or "?"
                snip = (c.get("snippet") or "")[:160]
                bits.append(f"<b>{src}</b><br>{snip}")
            self._add_bubble("<br><br>".join(bits), "info")

    def _on_error(self, msg: str):
        self.send_btn.setEnabled(True)
        self.send_btn.setText(t("chat.send"))
        self._add_bubble(t("chat.error") + msg, "info")


class SettingsView(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 20, 28, 24)
        lay.setSpacing(10)

        self.title = QLabel(t("header.settings"))
        self.title.setObjectName("pageTitle")
        self.sub = QLabel(t("header.settings.sub"))
        self.sub.setObjectName("pageSub")
        lay.addWidget(self.title)
        lay.addWidget(self.sub)

        lay.addWidget(QLabel(t("st.language")))
        lay.addWidget(QLabel(t("st.language_desc")))
        self.lang = QComboBox()
        self.lang.addItem(t("onboard.lang_zh"), "zh")
        self.lang.addItem(t("onboard.lang_en"), "en")
        self.lang.currentIndexChanged.connect(self._on_lang)
        lay.addWidget(self.lang)

        lay.addWidget(QLabel(t("st.api")))
        self.base_url = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.model = QLineEdit()
        for w, ph in [
            (self.base_url, t("st.base_url")),
            (self.api_key, t("st.api_key")),
            (self.model, "deepseek-v4-pro"),
        ]:
            w.setPlaceholderText(ph)
            lay.addWidget(w)
        model_hint = QLabel("DeepSeek ids: deepseek-v4-pro · deepseek-flash (exact casing)")
        model_hint.setStyleSheet("color:#6b7280;font-size:11px;")
        lay.addWidget(model_hint)
        save = QPushButton(t("st.save_api"))
        save.clicked.connect(self._save_llm)
        lay.addWidget(save)

        lay.addWidget(QLabel(t("st.rag_behavior")))
        lay.addWidget(QLabel(t("st.rag_behavior_desc")))
        self.auto_retrieve = QCheckBox(t("st.auto_retrieve"))
        self.auto_retrieve.toggled.connect(self._on_auto_retrieve)
        lay.addWidget(self.auto_retrieve)

        lay.addWidget(QLabel(t("st.shared_root")))
        lay.addWidget(QLabel(t("st.shared_root_desc")))
        root_row = QHBoxLayout()
        self.shared_root = QLineEdit()
        browse = QPushButton(t("st.browse"))
        browse.clicked.connect(self._browse_dir)
        root_row.addWidget(self.shared_root, 1)
        root_row.addWidget(browse)
        lay.addLayout(root_row)
        save_g = QPushButton(t("st.save_g"))
        save_g.clicked.connect(self._save_g)
        lay.addWidget(save_g)

        lay.addWidget(QLabel(t("st.holdings")))
        lay.addWidget(QLabel(t("st.holdings_desc")))
        hold_row = QHBoxLayout()
        self.holdings = QLineEdit()
        pick_h = QPushButton(t("st.holdings_select"))
        pick_h.clicked.connect(self._select_holdings)
        hold_row.addWidget(self.holdings, 1)
        hold_row.addWidget(pick_h)
        lay.addLayout(hold_row)

        lay.addWidget(QLabel(t("st.identity")))
        self.name_edit = QLineEdit()
        lay.addWidget(self.name_edit)
        upd = QPushButton(t("st.update_name"))
        upd.clicked.connect(self._update_name)
        lay.addWidget(upd)

        lay.addWidget(QLabel(t("st.service")))
        row = QHBoxLayout()
        self.start_btn = QPushButton(t("st.start"))
        self.stop_btn = QPushButton(t("st.stop"))
        self.start_btn.clicked.connect(self.main.ensure_rag_service)
        self.stop_btn.clicked.connect(self.main.stop_rag_service)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        lay.addLayout(row)
        self.status = QLabel("")
        lay.addWidget(self.status)

        lay.addWidget(QLabel(t("st.outlook")))
        lay.addWidget(QLabel(t("st.outlook_desc")))
        clear_o = QPushButton(t("st.clear_outlook"))
        clear_o.clicked.connect(self._clear_outlook_login)
        lay.addWidget(clear_o)

        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

    def retranslate(self):
        self.title.setText(t("header.settings"))
        self.sub.setText(t("header.settings.sub"))

    def _load_values(self):
        llm = get_llm_config()
        self.base_url.setText(llm.get("base_url") or "")
        self.api_key.setText(llm.get("api_key") or "")
        self.model.setText(llm.get("model") or "")
        cfg = load_app_config()
        self.auto_retrieve.blockSignals(True)
        self.auto_retrieve.setChecked(bool(cfg.get("auto_retrieve", True)))
        self.auto_retrieve.blockSignals(False)
        self.shared_root.setText(get_shared_root())
        self.holdings.setText(get_holdings_csv_path() or t("st.holdings_none"))
        self.name_edit.setText(get_display_name() or "")
        lang = get_language()
        self.lang.blockSignals(True)
        self.lang.setCurrentIndex(0 if lang == "zh" else 1)
        self.lang.blockSignals(False)

    def _on_lang(self):
        set_language(self.lang.currentData() or "zh")
        if hasattr(self.main, "retranslate_all"):
            self.main.retranslate_all()

    def _on_auto_retrieve(self, checked: bool):
        update_app_config(auto_retrieve=bool(checked))
        self.main._push_config_async()

    def _save_llm(self):
        normalized = normalize_llm_model(self.model.text())
        self.model.setText(normalized)
        update_app_config(
            llm_base_url=self.base_url.text().strip(),
            llm_api_key=self.api_key.text().strip(),
            llm_model=normalized,
        )
        self.main._push_config_async()
        QMessageBox.information(self, t("st.saved"), t("st.saved_push"))

    def _browse_dir(self):
        path = QFileDialog.getExistingDirectory(self, t("st.browse_tip"), self.shared_root.text())
        if path:
            self.shared_root.setText(path)

    def _save_g(self):
        set_shared_root(self.shared_root.text().strip())
        QMessageBox.information(self, t("st.saved"), t("st.paths_saved"))

    def _select_holdings(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t("st.holdings_select"), "", "CSV (*.csv);;All (*.*)"
        )
        if path:
            set_holdings_csv_path(path)
            self.holdings.setText(path)
            QMessageBox.information(self, t("st.saved"), t("st.holdings_saved"))

    def _update_name(self):
        name = self.name_edit.text().strip()
        if not name:
            return
        set_display_name(name)
        self.main.update_user_badge()
        QMessageBox.information(self, t("st.saved"), t("st.name_updated", name=name))

    def _clear_outlook_login(self):
        import shutil

        roots = [_ROOT / ".edge_profile", _ROOT / ".edge_profiles"]
        cleared, failed = [], []
        for p in roots:
            if not p.exists():
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=False)
                cleared.append(str(p))
            except Exception as e:
                failed.append(f"{p}: {e}")
        if failed:
            QMessageBox.warning(self, t("st.outlook"), t("st.clear_outlook_fail", failed="\n".join(failed)))
        elif cleared:
            QMessageBox.information(self, t("st.outlook"), t("st.clear_outlook_ok", paths="\n".join(cleared)))
        else:
            QMessageBox.information(self, t("st.outlook"), t("st.clear_outlook_none"))


class PlaceholderView(QWidget):
    def __init__(self, title_key: str, sub_key: str):
        super().__init__()
        self.title_key = title_key
        self.sub_key = sub_key
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 16)
        self.title = QLabel(t(title_key))
        self.title.setObjectName("pageTitle")
        self.sub = QLabel(t(sub_key))
        self.sub.setObjectName("pageSub")
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addStretch(1)

    def retranslate(self):
        self.title.setText(t(self.title_key))
        self.sub.setText(t(self.sub_key))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QP Helper — Quam Plus AI Research")
        self.resize(1240, 800)
        self.rag_manager = RagServiceManager()
        self.rag_client: RagClient | None = None
        self.popup_manager: PopupManager | None = None
        self._views: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self._build_menus()
        self._init_services()

    def _build_menus(self):
        """Install a native macOS menu bar (and a harmless menu on other OSes)."""
        menubar = self.menuBar()
        app_menu = menubar.addMenu(t("brand.name") if is_macos() else "&File")

        about = QAction(t("brand.name"), self)
        about.triggered.connect(
            lambda: QMessageBox.about(
                self,
                t("brand.name"),
                f"{t('brand.name')}\n{t('brand.sub')}",
            )
        )
        about.setMenuRole(QAction.MenuRole.AboutRole)
        app_menu.addAction(about)

        quit_act = QAction(t("common.close") if not is_macos() else "Quit QP Helper", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(QApplication.instance().quit)
        quit_act.setMenuRole(QAction.MenuRole.QuitRole)
        app_menu.addAction(quit_act)

        edit = menubar.addMenu("&Edit")
        for role, key, label in (
            (QAction.MenuRole.NoRole, QKeySequence.StandardKey.Undo, "Undo"),
            (QAction.MenuRole.NoRole, QKeySequence.StandardKey.Redo, "Redo"),
            (QAction.MenuRole.NoRole, QKeySequence.StandardKey.Cut, "Cut"),
            (QAction.MenuRole.NoRole, QKeySequence.StandardKey.Copy, "Copy"),
            (QAction.MenuRole.NoRole, QKeySequence.StandardKey.Paste, "Paste"),
            (QAction.MenuRole.NoRole, QKeySequence.StandardKey.SelectAll, "Select All"),
        ):
            act = QAction(label, self)
            act.setShortcut(key)
            act.setMenuRole(role)
            edit.addAction(act)

        # Hide the in-window menu bar on macOS — items merge into the system bar.
        if is_macos():
            menubar.setNativeMenuBar(True)

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(12, 16, 12, 12)
        logo = QLabel("Q+")
        logo.setStyleSheet(
            "background:#c9a227;color:#1a1408;border-radius:14px;font-weight:800;"
            "padding:8px;qproperty-alignment: AlignCenter;"
        )
        logo.setFixedSize(40, 40)
        sb.addWidget(logo)
        brand = QLabel(t("brand.name"))
        brand.setStyleSheet("font-weight:700;padding-top:8px;")
        sb.addWidget(brand)
        sb.addSpacing(12)

        navs = [
            ("home", "nav.home"),
            ("database", "nav.database"),
            ("emails", "nav.emails"),
            ("upload_history", "nav.upload_history"),
            ("prompt_mgmt", "nav.prompt_mgmt"),
            ("settings", "nav.settings"),
        ]
        for key, ik in navs:
            btn = QPushButton(t(ik))
            btn.setProperty("class", "nav")
            btn.setProperty("active", "false")
            btn.setProperty("i18n_key", ik)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self.navigate_to(k))
            # apply class via stylesheet selector workaround
            btn.setStyleSheet("")
            btn.setObjectName("navBtn")
            btn.setProperty("cssClass", "nav")
            self._nav_buttons[key] = btn
            sb.addWidget(btn)
        # style nav buttons
        for btn in self._nav_buttons.values():
            btn.setProperty("class", "nav")
            btn.setStyleSheet(
                "QPushButton{text-align:left;padding:10px 14px;border:none;border-radius:10px;"
                "color:#aeb4c0;background:transparent;}"
                "QPushButton:hover{background:#1d2330;color:#fff;}"
            )

        sb.addStretch(1)
        self.user_badge = QLabel(get_actor())
        self.user_badge.setStyleSheet("color:#8b93a7;padding:8px;")
        sb.addWidget(self.user_badge)
        main_layout.addWidget(sidebar)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        self._views["home"] = HomeView(self)
        self._views["database"] = DatabaseView(self)
        self._views["emails"] = EmailFetchView(self)
        self._views["upload_history"] = UploadHistoryView(self)
        self._views["prompt_mgmt"] = PromptMgmtView(self)
        self._views["settings"] = SettingsView(self)
        for v in self._views.values():
            self.stack.addWidget(v)
        self.navigate_to("home")

    def _init_services(self):
        port = get_rag_service_port()
        self.rag_client = RagClient(f"http://127.0.0.1:{port}")
        QTimer.singleShot(200, self.ensure_rag_service)

    def ensure_rag_service(self):
        ok, msg = self.rag_manager.start()
        if not ok and "already" not in msg:
            # still probe — service may already be up
            pass
        self._schedule_probe(push=True)

    def _schedule_probe(self, push: bool = False, attempt: int = 1):
        base_url = f"http://127.0.0.1:{get_rag_service_port()}"
        push_cfg = None
        if push:
            llm = get_llm_config()
            cfg = load_app_config()
            push_cfg = {
                "base_url": llm.get("base_url") or None,
                "api_key": llm.get("api_key") or None,
                "model": llm.get("model") or None,
                "auto_retrieve": bool(cfg.get("auto_retrieve", True)),
            }
        probe = ServiceProbe(base_url, push_cfg)
        probe.result.connect(lambda alive: self._on_probe_result(alive, attempt))
        probe.start()
        self._last_probe = probe

    def _on_probe_result(self, alive: bool, attempt: int):
        self._update_service_status(alive)
        if alive:
            if self.popup_manager is None and self.rag_client is not None:
                self.popup_manager = PopupManager(self.rag_client, self)
                self.popup_manager.start()
            return
        if attempt < 8:
            QTimer.singleShot(1500, lambda: self._schedule_probe(push=True, attempt=attempt + 1))

    def _push_config_async(self):
        self._schedule_probe(push=True)

    def stop_rag_service(self):
        if self.popup_manager:
            self.popup_manager.stop()
        self.rag_manager.stop()
        self._update_service_status(False)

    def _update_service_status(self, alive: bool):
        home = self._views.get("home")
        if isinstance(home, HomeView):
            home.set_service_alive(alive)

    def navigate_to(self, key: str):
        view = self._views.get(key)
        if not view:
            return
        if hasattr(view, "refresh"):
            try:
                view.refresh()
            except Exception:
                pass
        self.stack.setCurrentWidget(view)
        for k, btn in self._nav_buttons.items():
            active = k == key
            if active:
                btn.setStyleSheet(
                    "QPushButton{text-align:left;padding:10px 14px;border:1px solid #3d3420;"
                    "border-radius:10px;color:#f0c674;background:#2a2418;}"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton{text-align:left;padding:10px 14px;border:none;border-radius:10px;"
                    "color:#aeb4c0;background:transparent;}"
                    "QPushButton:hover{background:#1d2330;color:#fff;}"
                )

    @property
    def views(self) -> dict[str, QWidget]:
        """Alias used by recovered PromptMgmtView / DatabaseView (mw.views)."""
        return self._views

    def retranslate_all(self):
        for btn in self._nav_buttons.values():
            ik = btn.property("i18n_key")
            if ik:
                btn.setText(t(ik))
        for view in self._views.values():
            if hasattr(view, "retranslate"):
                try:
                    view.retranslate()
                except Exception:
                    pass
        self.update_user_badge()

    def update_user_badge(self):
        self.user_badge.setText(get_actor())

    def closeEvent(self, event):
        try:
            if self.popup_manager:
                self.popup_manager.stop()
            # keep RAG service running across restarts? original reaps on close
            self.rag_manager.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    # High-DPI / Retina: set before QApplication on older Qt; harmless on Qt6.
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    apply_qt_macos_tweaks(app)
    app.setStyleSheet(_load_stylesheet())
    if not get_display_name():
        dlg = OnboardingDialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
    win = MainWindow()
    win.show()
    if is_macos():
        # Raise / activate so a double-clicked .app does not stay behind Finder.
        win.raise_()
        win.activateWindow()
    if not get_llm_config().get("api_key"):
        QMessageBox.information(win, t("startup.no_api_title"), t("startup.no_api_msg"))
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise

