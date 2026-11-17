"""Auto-popup background system (reconstructed stub + Toast)."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFrame, QApplication

from config_manager import get_popup_config
from rag_client import RagClient
from audit_logger import log_action


def _normalize_hits(raw: dict | list) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    # chroma-like
    if raw.get("ids") and isinstance(raw["ids"], list):
        ids = raw["ids"][0] if raw["ids"] else []
        docs = (raw.get("documents") or [[]])[0]
        metas = (raw.get("metadatas") or [[]])[0]
        dists = (raw.get("distances") or [[]])[0]
        out = []
        for i, cid in enumerate(ids):
            dist = dists[i] if i < len(dists) else None
            score = (1.0 - dist) if dist is not None else 0.0
            meta = metas[i] if i < len(metas) else {}
            text = docs[i] if i < len(docs) else ""
            out.append({"chunk_id": cid, "text": text, "score": score, "metadata": meta,
                        "g_path": (meta or {}).get("source", "")})
        return out
    return list(raw.get("hits") or [])


class Toast(QWidget):
    """A small temporary message shown near the parent widget."""

    def __init__(self, parent=None, message: str = "", duration: int = 2200):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(
            """
            #toastCard {
                background: #f5f0d8;
                border: 1px solid #d4af37;
                border-radius: 10px;
            }
            QLabel { color: #1a1a1a; font-size: 13px; background: transparent; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("toastCard")
        card_layout = QVBoxLayout(card)
        label = QLabel(message)
        label.setWordWrap(True)
        card_layout.addWidget(label)
        layout.addWidget(card)
        self.adjustSize()
        if parent is not None:
            geo = parent.geometry()
            x = geo.center().x() - self.width() // 2
            y = geo.bottom() - self.height() - 40
            self.move(max(0, x), max(0, y))
        QTimer.singleShot(duration, self.close)

    def showEvent(self, event):
        super().showEvent(event)


class PopupRetrievalThread(QThread):
    result = pyqtSignal(list)

    def __init__(self, client: RagClient, query: str, parent=None):
        super().__init__(parent)
        self.client = client
        self.query = query

    def run(self):
        try:
            raw = self.client.retrieve(self.query, top_k=3)
            self.result.emit(_normalize_hits(raw))
        except Exception:
            self.result.emit([])


class PopupWindow(QWidget):
    def __init__(self, title: str, snippet: str, g_path: str, parent=None):
        super().__init__(parent)
        self.g_path = g_path
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setStyleSheet(
            "QWidget{background:#1f2430;color:#eee;border:1px solid #d4af37;border-radius:10px;}"
            "QPushButton{background:#d4af37;color:#111;border:none;padding:6px 12px;border-radius:6px;}"
        )
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(title))
        sn = QLabel(snippet[:280])
        sn.setWordWrap(True)
        lay.addWidget(sn)
        row = QHBoxLayout()
        open_btn = QPushButton("Open")
        ignore_btn = QPushButton("Ignore")
        open_btn.clicked.connect(self._open)
        ignore_btn.clicked.connect(self.close)
        row.addWidget(open_btn)
        row.addWidget(ignore_btn)
        lay.addLayout(row)
        self.resize(360, 160)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

    def _open(self):
        from platform_compat import open_path

        log_action("popup_opened", g_paths=[self.g_path])
        try:
            if self.g_path:
                open_path(self.g_path)
        except Exception:
            pass
        self.close()


class PopupManager:
    """Background monitor; reconstructed simplified version."""

    def __init__(self, client: RagClient, parent=None):
        self.client = client
        self.parent = parent
        self._timer = QTimer(parent)
        self._timer.timeout.connect(self._tick)
        self._last_query = ""
        self._cooldown_until = 0.0
        self._thread: PopupRetrievalThread | None = None

    def start(self):
        cfg = get_popup_config()
        if not cfg.get("enabled", True):
            return
        self._timer.start(8000)

    def stop(self):
        self._timer.stop()

    def _tick(self):
        import time
        cfg = get_popup_config()
        if not cfg.get("enabled", True):
            return
        if time.time() < self._cooldown_until:
            return
        # Simplified: no Win32 window title scraping in this reconstruction.
        return

    def show_hit(self, hit: dict[str, Any]):
        import time
        cfg = get_popup_config()
        score = float(hit.get("score") or 0)
        if score < float(cfg.get("threshold") or 0.75):
            return
        title = (hit.get("metadata") or {}).get("source") or hit.get("g_path") or "Document"
        win = PopupWindow(str(title), str(hit.get("text") or ""), str(hit.get("g_path") or ""), self.parent)
        win.show()
        log_action("popup_triggered", metadata={"score": score, "source": title})
        self._cooldown_until = time.time() + int(cfg.get("cooldown_seconds") or 120)
