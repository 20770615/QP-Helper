"""Upload history UI — recovered from qp_email_helper bytecode (§4.4 / §6)."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import shared_store
from i18n import t


class UploadHistoryView(QWidget):
    """Upload history — recent 10 uploads (ui_reorg_prompt §4.4 / §6)."""

    def __init__(self, main_window: "MainWindow"):  # noqa: F821
        super().__init__()
        self.mw = main_window
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch()

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, 1)

    def refresh(self) -> None:
        # Keep the trailing stretch; remove widgets above it.
        while self.list_layout.count() > 1:
            w = self.list_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

        records = shared_store.read_uploads(10)
        if not records:
            empty = QLabel(t("uh.empty"))
            empty.setStyleSheet(
                "color: #999; font-size: 13px; padding: 30px; text-align: center;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self.list_layout.insertWidget(0, empty)
            return

        for r in records:
            b = QFrame()
            b.setObjectName("recordBubble")
            bl = QVBoxLayout(b)
            bl.setContentsMargins(14, 12, 14, 12)
            txt = QLabel(
                t(
                    "uh.record",
                    user=r.get("user", ""),
                    time=r.get("time", ""),
                    file=r.get("filename", ""),
                )
            )
            txt.setWordWrap(True)
            txt.setStyleSheet("font-size: 13px; color: #1a1a2e;")
            bl.addWidget(txt)
            self.list_layout.insertWidget(self.list_layout.count() - 1, b)
