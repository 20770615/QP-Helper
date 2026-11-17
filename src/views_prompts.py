"""Prompt management view (simplified recovery of PromptMgmtView + PromptDialog)."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import shared_store
from i18n import t


class PromptDialog(QDialog):
    """Create / edit a prompt (ui_reorg_prompt §4.5)."""

    def __init__(self, parent=None, title: str = "", content: str = ""):
        super().__init__(parent)
        self.title = title or ""
        self.content = content or ""
        self.setWindowTitle(t("pm.detail"))
        self.setMinimumWidth(480)
        self.setMinimumHeight(360)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(t("pm.title_ph")))
        self.title_edit = QLineEdit(self.title)
        self.title_edit.setPlaceholderText(t("pm.title_ph"))
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel(t("pm.content_ph")))
        self.content_edit = QTextEdit()
        self.content_edit.setPlainText(self.content)
        self.content_edit.setPlaceholderText(t("pm.content_ph"))
        layout.addWidget(self.content_edit, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton(t("pm.save"))
        ok.setObjectName("primaryBtn")
        ok.clicked.connect(self.accept)
        cancel = QPushButton(t("pm.cancel"))
        cancel.setObjectName("secondaryBtn")
        cancel.clicked.connect(self.reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        layout.addLayout(row)

    def accept(self):
        title = self.title_edit.text().strip()
        if not title:
            self.title_edit.setStyleSheet("border: 1px solid #ef4444;")
            return
        self.title_edit.setStyleSheet("")
        self.title = title
        self.content = self.content_edit.toPlainText()
        super().accept()


class PromptMgmtView(QWidget):
    """Prompt management — shared bubble library (ui_reorg_prompt §4.5 / §6)."""

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)

        head = QVBoxLayout()
        self.page_title = QLabel(t("header.prompt_mgmt"))
        self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel(t("header.prompt_mgmt.sub"))
        self.page_sub.setObjectName("pageSub")
        head.addWidget(self.page_title)
        head.addWidget(self.page_sub)
        layout.addLayout(head)

        tb = QHBoxLayout()
        tb.addStretch(1)
        self.new_btn = QPushButton(t("pm.new"))
        self.new_btn.setObjectName("primaryBtn")
        self.new_btn.setMinimumHeight(32)
        self.new_btn.clicked.connect(self._new_prompt)
        tb.addWidget(self.new_btn)
        layout.addLayout(tb)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.list_layout.setSpacing(10)
        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, 1)
        self.retranslate()

    def retranslate(self):
        self.page_title.setText(t("header.prompt_mgmt"))
        self.page_sub.setText(t("header.prompt_mgmt.sub"))
        self.new_btn.setText(t("pm.new"))

    def refresh(self):
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        prompts = shared_store.load_prompts()
        if not prompts:
            empty = QLabel(t("pm.empty"))
            empty.setStyleSheet("color:#999;font-size:13px;padding:30px;text-align:center;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self.list_layout.addWidget(empty)
            return

        for p in prompts:
            self.list_layout.addWidget(self._make_bubble(p))
        self.list_layout.addStretch(1)

    def _make_bubble(self, p: dict) -> QFrame:
        b = QFrame()
        b.setObjectName("promptBubble")
        b.setStyleSheet(
            "QFrame#promptBubble{background:#151821;border:1px solid #2a3140;border-radius:12px;}"
        )
        bl = QVBoxLayout(b)
        bl.setContentsMargins(14, 12, 14, 12)
        bl.setSpacing(8)

        title = QLabel(p.get("title") or "")
        title.setObjectName("promptTitle")
        title.setStyleSheet("font-weight:700;color:#e8eaed;")
        title.setWordWrap(True)
        bl.addWidget(title)

        content = QLabel(p.get("content") or "")
        content.setObjectName("promptContent")
        content.setWordWrap(True)
        content.setMaximumHeight(120)
        content.setStyleSheet("color:#aeb4c0;")
        bl.addWidget(content)

        al = QHBoxLayout()
        pid = p.get("id") or ""
        body = p.get("content") or ""

        edit_btn = QPushButton(t("pm.edit"))
        edit_btn.setObjectName("secondaryBtn")
        edit_btn.clicked.connect(lambda _=False, i=pid: self._edit_prompt(i))
        al.addWidget(edit_btn)

        del_btn = QPushButton(t("pm.delete"))
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(lambda _=False, i=pid: self._delete_prompt(i))
        al.addWidget(del_btn)

        copy_btn = QPushButton(t("pm.copy"))
        copy_btn.clicked.connect(lambda _=False, c=body, btn=copy_btn: self._copy_text(c, btn))
        al.addWidget(copy_btn)

        use_btn = QPushButton(t("pm.use"))
        use_btn.setObjectName("primaryBtn")
        use_btn.clicked.connect(lambda _=False, c=body: self._use_in_chat(c))
        al.addWidget(use_btn)

        al.addStretch(1)
        bl.addLayout(al)

        b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        b.customContextMenuRequested.connect(
            lambda _pos, i=pid, c=body: self._prompt_context_menu(i, c)
        )
        return b

    def _prompt_context_menu(self, pid: str, content: str):
        menu = QMenu(self)
        menu.addAction(t("pm.edit"), lambda: self._edit_prompt(pid))
        menu.addAction(t("pm.copy"), lambda: self._copy_text(content, None))
        menu.addAction(t("pm.delete"), lambda: self._delete_prompt(pid))
        menu.exec(QCursor.pos())

    def _new_prompt(self):
        dlg = PromptDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        shared_store.add_prompt(dlg.title, dlg.content)
        self.refresh()

    def _edit_prompt(self, pid: str):
        p = next((x for x in shared_store.load_prompts() if x.get("id") == pid), None)
        if not p:
            return
        dlg = PromptDialog(self, title=p.get("title") or "", content=p.get("content") or "")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        shared_store.update_prompt(pid, dlg.title, dlg.content)
        self.refresh()

    def _delete_prompt(self, pid: str):
        ans = QMessageBox.question(
            self,
            t("common.confirm"),
            t("pm.confirm_delete"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        shared_store.delete_prompt(pid)
        self.refresh()

    def _use_in_chat(self, content: str):
        views = getattr(self.mw, "views", None) or getattr(self.mw, "_views", {})
        home = views.get("home") if isinstance(views, dict) else None
        if home is not None and hasattr(home, "set_input"):
            home.set_input(content)
        if hasattr(self.mw, "navigate_to"):
            self.mw.navigate_to("home")

    def _copy_text(self, text: str, btn: QPushButton | None):
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text or "")
        if btn is not None:
            original = btn.text()
            btn.setText(t("chat.copied"))
            QTimer.singleShot(1200, lambda: btn.setText(original))
