"""Database / knowledge-base browser (simplified recovery of DatabaseView).

Covers the core of the ~73-method inventory: folder/file browsing, upload,
ingest KB, remove-from-DB, open/delete, and basic tags. Band-selection,
clipboard copy/paste, and full TagDialog UI are omitted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import shared_store
from audit_logger import log_action
from i18n import t, tag_display_name
from platform_compat import open_path, reveal_in_file_manager

if TYPE_CHECKING:
    from rag_client import RagClient


# ── Background workers ───────────────────────────────────────────────────────


class IngestWorker(QThread):
    """Background ingestion (dir / files / emails)."""

    done = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, client: "RagClient", mode: str, path: str):
        super().__init__()
        self.client = client
        self.mode = mode
        self.path = path

    def run(self):
        try:
            if self.mode == "emails":
                resp = self.client.ingest_emails(self.path)
            elif self.mode == "dir":
                resp = self.client.ingest_directory(self.path)
            else:
                paths = json.loads(self.path) if isinstance(self.path, str) else list(self.path)
                resp = self.client.ingest_files(paths)
            self.done.emit(resp if isinstance(resp, dict) else {})
        except Exception as e:
            self.error.emit(str(e))


class RemoveWorker(QThread):
    """Remove vector entries while keeping local files."""

    done = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, client: "RagClient", paths: list[str], user_id: str = "shared", scope: str = "shared"):
        super().__init__()
        self.client = client
        self.paths = paths
        self.user_id = user_id
        self.scope = scope

    def run(self):
        try:
            resp = self.client.remove_from_db(self.paths, user_id=self.user_id, scope=self.scope)
            self.done.emit(resp)
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")


# ── Main view ────────────────────────────────────────────────────────────────


class DatabaseView(QWidget):
    """Team-shared knowledge base browser (ui_reorg_prompt §4.2 / §6)."""

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._current_folder: str = ""
        self._current_tag: str = ""
        self._all_files: list[dict[str, Any]] = []
        self._folder_btns: list[QPushButton] = []
        self._kb_worker: IngestWorker | None = None
        self._upload_worker: IngestWorker | None = None
        self._remove_worker: RemoveWorker | None = None
        self._build_ui()
        self.setAcceptDrops(True)
        self.refresh()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Left sidebar — folders
        sub = QFrame()
        sub.setObjectName("subSidebar")
        sub.setFixedWidth(200)
        sl = QVBoxLayout(sub)
        sl.setContentsMargins(10, 14, 10, 14)
        sl.setSpacing(6)

        self.sub_title = QLabel(t("db.folders"))
        self.sub_title.setStyleSheet("font-size:13px;font-weight:700;color:#aeb4c0;padding:0 4px;")
        sl.addWidget(self.sub_title)

        self.all_btn = QPushButton(t("db.all_files"))
        self.all_btn.setObjectName("subNavBtn")
        self.all_btn.setCheckable(True)
        self.all_btn.setChecked(True)
        self.all_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.all_btn.clicked.connect(lambda: self._select_folder(""))
        sl.addWidget(self.all_btn)

        self.folder_scroll = QScrollArea()
        self.folder_scroll.setWidgetResizable(True)
        self.folder_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.folder_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.folder_container = QWidget()
        self.folder_layout = QVBoxLayout(self.folder_container)
        self.folder_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_layout.setSpacing(2)
        self.folder_scroll.setWidget(self.folder_container)
        sl.addWidget(self.folder_scroll, 1)
        layout.addWidget(sub)

        # Main pane
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(20, 16, 20, 16)
        ml.setSpacing(10)

        head = QVBoxLayout()
        self.page_title = QLabel(t("header.database"))
        self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel(t("header.database.sub"))
        self.page_sub.setObjectName("pageSub")
        head.addWidget(self.page_title)
        head.addWidget(self.page_sub)
        ml.addLayout(head)

        # Toolbar
        tb = QHBoxLayout()
        self.new_btn = QPushButton(t("db.new_folder"))
        self.new_btn.setObjectName("secondaryBtn")
        self.new_btn.clicked.connect(self._new_folder)
        tb.addWidget(self.new_btn)

        self.upload_btn = QPushButton(t("db.upload"))
        self.upload_btn.setObjectName("primaryBtn")
        self.upload_btn.setToolTip(t("db.upload_title"))
        self.upload_btn.clicked.connect(self._upload)
        tb.addWidget(self.upload_btn)

        self.ingest_kb_btn = QPushButton(t("db.ingest_kb"))
        self.ingest_kb_btn.setToolTip(t("db.ingest_kb_title"))
        self.ingest_kb_btn.clicked.connect(self._ingest_kb)
        tb.addWidget(self.ingest_kb_btn)

        tb.addStretch(1)

        self.tag_filter = QComboBox()
        self.tag_filter.setMinimumHeight(28)
        self.tag_filter.setMinimumWidth(140)
        self.tag_filter.currentIndexChanged.connect(self._on_filter)
        tb.addWidget(QLabel(t("db.filter_tag")))
        tb.addWidget(self.tag_filter)
        ml.addLayout(tb)

        # Breadcrumb / current path
        self.crumb_label = QLabel("")
        self.crumb_label.setStyleSheet("color:#8b93a7;font-size:12px;")
        ml.addWidget(self.crumb_label)

        # Subfolders row
        self.sub_scroll = QScrollArea()
        self.sub_scroll.setWidgetResizable(True)
        self.sub_scroll.setFixedHeight(40)
        self.sub_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sub_container = QWidget()
        self.sub_layout = QHBoxLayout(self.sub_container)
        self.sub_layout.setContentsMargins(0, 0, 0, 0)
        self.sub_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.sub_scroll.setWidget(self.sub_container)
        ml.addWidget(self.sub_scroll)

        # Drop hint + file list
        self.drop = QLabel(t("db.drop_hint"))
        self.drop.setObjectName("dropArea")
        self.drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop.setWordWrap(True)
        self.drop.setStyleSheet(
            "color:#8b93a7;border:1px dashed #3a4152;border-radius:10px;padding:10px;"
        )
        ml.addWidget(self.drop)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.files_widget = QWidget()
        self.files_layout = QVBoxLayout(self.files_widget)
        self.files_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.files_layout.setSpacing(8)
        self.scroll.setWidget(self.files_widget)
        ml.addWidget(self.scroll, 1)

        self.empty_label = QLabel(t("db.pick_folder"))
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color:#8b93a7;padding:24px;")
        self.files_layout.addWidget(self.empty_label)

        layout.addWidget(main, 1)
        self.retranslate()

    def retranslate(self):
        self.sub_title.setText(t("db.folders"))
        self.all_btn.setText(t("db.all_files"))
        self.page_title.setText(t("header.database"))
        self.page_sub.setText(t("header.database.sub"))
        self.new_btn.setText(t("db.new_folder"))
        self.upload_btn.setText(t("db.upload"))
        self.upload_btn.setToolTip(t("db.upload_title"))
        self.ingest_kb_btn.setText(t("db.ingest_kb"))
        self.ingest_kb_btn.setToolTip(t("db.ingest_kb_title"))
        self.drop.setText(t("db.drop_hint"))
        self._populate_tags()
        self._update_crumb()

    # ── Folders / refresh ─────────────────────────────────────────────────────

    def refresh(self):
        while self.folder_layout.count():
            item = self.folder_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._folder_btns.clear()

        try:
            folders = shared_store.list_folders()
        except Exception:
            folders = []

        for f in folders:
            btn = QPushButton(f)
            btn.setObjectName("subNavBtn")
            btn.setCheckable(True)
            btn.setChecked(self._current_folder == f or self._current_folder.startswith(f + "/"))
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _=False, name=f: self._select_folder(name))
            self.folder_layout.addWidget(btn)
            self._folder_btns.append(btn)

        self.all_btn.setChecked(not self._current_folder)
        self._populate_tags()
        self._render_files()

    def _select_folder(self, folder: str):
        self._current_folder = folder or ""
        self.all_btn.setChecked(not self._current_folder)
        for btn in self._folder_btns:
            top = btn.text()
            btn.setChecked(
                bool(self._current_folder)
                and (self._current_folder == top or self._current_folder.startswith(top + "/"))
            )
        self._render_files()

    def _populate_tags(self):
        cur = self._current_tag
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        self.tag_filter.addItem(t("db.all_files"), "")
        try:
            for tag in shared_store.all_tags():
                self.tag_filter.addItem(tag_display_name(tag), tag)
        except Exception:
            pass
        idx = self.tag_filter.findData(cur)
        self.tag_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.tag_filter.blockSignals(False)

    def _on_filter(self, _index: int = 0):
        self._current_tag = self.tag_filter.currentData() or ""
        self._apply_and_render()

    def _update_crumb(self):
        if self._current_folder:
            self.crumb_label.setText(self._current_folder.replace("/", " › "))
        else:
            self.crumb_label.setText(t("db.all_files"))

    def _set_subfolders(self):
        while self.sub_layout.count():
            item = self.sub_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        try:
            names = shared_store.list_subfolders(self._current_folder or None)
        except Exception:
            names = []
        if self._current_folder:
            up = QPushButton(t("db.up_level"))
            up.setFlat(True)
            parent = self._current_folder.rsplit("/", 1)[0] if "/" in self._current_folder else ""
            up.clicked.connect(lambda _=False, p=parent: self._select_folder(p))
            self.sub_layout.addWidget(up)
        for name in names:
            rel = f"{self._current_folder}/{name}" if self._current_folder else name
            btn = QPushButton(name)
            btn.setFlat(True)
            btn.clicked.connect(lambda _=False, r=rel: self._select_folder(r))
            self.sub_layout.addWidget(btn)
        self.sub_layout.addStretch(1)

    # ── File list ─────────────────────────────────────────────────────────────

    def _clear_rendered(self):
        while self.files_layout.count():
            item = self.files_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _render_files(self):
        self._update_crumb()
        self._set_subfolders()
        folder = self._current_folder or None
        try:
            self._all_files = shared_store.list_files(folder)
        except Exception:
            self._all_files = []
        self._apply_and_render()

    def _apply_and_render(self):
        self._clear_rendered()
        files = list(self._all_files)
        if self._current_tag:
            files = [f for f in files if self._current_tag in (f.get("tags") or [])]

        if not files:
            if not self._all_files:
                msg = t("db.empty") if self._current_folder else t("db.empty_kb")
            else:
                msg = t("db.empty")
            lab = QLabel(msg)
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet("color:#8b93a7;padding:24px;")
            lab.setWordWrap(True)
            self.files_layout.addWidget(lab)
            return

        for f in files:
            self.files_layout.addWidget(self._make_bubble(f))
        self.files_layout.addStretch(1)

    def _make_bubble(self, f: dict[str, Any]) -> QFrame:
        b = QFrame()
        b.setObjectName("fileBubble")
        b.setStyleSheet(
            "QFrame#fileBubble{background:#151821;border:1px solid #2a3140;border-radius:12px;}"
        )
        bl = QVBoxLayout(b)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.setSpacing(6)

        name = f.get("name") or ""
        title = QLabel(name)
        title.setStyleSheet("font-weight:600;color:#e8eaed;")
        title.setWordWrap(True)
        bl.addWidget(title)

        meta_bits = []
        folder = f.get("folder") or ""
        if folder:
            meta_bits.append(folder)
        tags = f.get("tags") or []
        if tags:
            meta_bits.append(" · ".join(tag_display_name(tg) for tg in tags))
        if meta_bits:
            meta = QLabel("  |  ".join(meta_bits))
            meta.setStyleSheet("color:#8b93a7;font-size:12px;")
            meta.setWordWrap(True)
            bl.addWidget(meta)

        row = QHBoxLayout()
        open_btn = QPushButton(t("db.open"))
        open_btn.setFlat(True)
        open_btn.clicked.connect(lambda _=False, p=f.get("abs_path"): self._open_file(p))
        row.addWidget(open_btn)

        loc_btn = QPushButton(t("db.open_location"))
        loc_btn.setFlat(True)
        loc_btn.clicked.connect(lambda _=False, p=f.get("abs_path"): self._open_location(p))
        row.addWidget(loc_btn)

        tag_btn = QPushButton(t("db.set_tags"))
        tag_btn.setFlat(True)
        tag_btn.clicked.connect(lambda _=False, file=f: self._edit_tags(file))
        row.addWidget(tag_btn)

        rm_db = QPushButton(t("db.remove_from_db"))
        rm_db.setFlat(True)
        rm_db.clicked.connect(
            lambda _=False, rel=f.get("rel_path"): self._remove_from_db([rel] if rel else [])
        )
        row.addWidget(rm_db)

        del_btn = QPushButton(t("db.delete"))
        del_btn.setObjectName("dangerBtn")
        del_btn.setFlat(True)
        del_btn.clicked.connect(lambda _=False, file=f: self._delete_file(file))
        row.addWidget(del_btn)

        row.addStretch(1)
        bl.addLayout(row)
        return b

    # ── File actions ──────────────────────────────────────────────────────────

    def _open_file(self, path: str | None):
        if not path:
            return
        try:
            open_path(path)
            log_action("open_doc", path=path)
        except Exception as e:
            QMessageBox.warning(self, t("common.error"), t("common.cannot_open") + f"\n{e}")

    def _open_location(self, path: str | None):
        """Open Explorer / Finder at the file's parent directory."""
        if not path:
            return
        try:
            p = Path(path).resolve()
            if not p.exists():
                QMessageBox.warning(self, t("common.error"), t("db.not_found", name=p.name))
                return
            reveal_in_file_manager(p)
        except Exception as e:
            QMessageBox.warning(self, t("common.error"), t("common.cannot_open") + f"\n{e}")

    def _edit_tags(self, f: dict[str, Any]):
        current = ", ".join(f.get("tags") or [])
        text, ok = QInputDialog.getText(
            self,
            t("db.tag_dialog", name=f.get("name") or ""),
            t("db.tag_prompt"),
            text=current,
        )
        if not ok:
            return
        tags = [x.strip() for x in text.split(",") if x.strip()]
        rel = f.get("rel_path")
        if not rel:
            return
        try:
            shared_store.write_tags(rel, tags)
        except Exception as e:
            QMessageBox.warning(self, t("common.error"), t("db.op_error", err=str(e)))
            return
        self._populate_tags()
        self._render_files()

    def _delete_file(self, f: dict[str, Any]):
        ans = QMessageBox.question(
            self,
            t("common.confirm"),
            t("db.confirm_delete", name=f.get("name") or ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            shared_store.delete_file(f.get("abs_path") or "")
        except Exception as e:
            QMessageBox.warning(self, t("common.error"), t("db.op_error", err=str(e)))
            return
        self._render_files()

    def _remove_from_db(self, rels: list[str]):
        client = getattr(self.mw, "rag_client", None)
        if not client or not getattr(client, "is_alive", lambda: False)():
            QMessageBox.warning(self, t("common.warning"), t("common.service_offline"))
            return
        rels = [r for r in rels if r]
        if not rels:
            return
        if len(rels) == 1:
            name = next(
                (f.get("name") for f in self._all_files if f.get("rel_path") == rels[0]),
                rels[0],
            )
            msg = t("db.confirm_remove_db", name=name)
        else:
            msg = t("db.confirm_remove_db_multi", n=len(rels))
        ans = QMessageBox.question(
            self,
            t("db.remove_from_db_title"),
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        abs_paths = []
        for rel in rels:
            hit = next((f for f in self._all_files if f.get("rel_path") == rel), None)
            if hit and hit.get("abs_path"):
                abs_paths.append(hit["abs_path"])
            else:
                abs_paths.append(str(shared_store.kb_root() / rel))

        self.ingest_kb_btn.setEnabled(False)
        self._remove_worker = RemoveWorker(client, abs_paths)
        self._remove_worker.done.connect(self._on_remove_db_done)
        self._remove_worker.error.connect(self._on_remove_db_error)
        self._remove_worker.start()

    def _on_remove_db_done(self, r: object):
        self.ingest_kb_btn.setEnabled(True)
        n = 0
        if isinstance(r, dict):
            removed = r.get("removed") or []
            n = len(removed) if isinstance(removed, list) else int(removed or 0)
        QMessageBox.information(
            self,
            t("common.ok"),
            t("db.removed_db_done", n=n or 1),
        )
        self._render_files()

    def _on_remove_db_error(self, e: str):
        self.ingest_kb_btn.setEnabled(True)
        QMessageBox.critical(self, t("db.remove_db_fail"), str(e))

    # ── New folder / upload / ingest ──────────────────────────────────────────

    def _new_folder(self):
        name, ok = QInputDialog.getText(self, t("db.new_folder"), t("db.folder_prompt"))
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        rel = f"{self._current_folder}/{name}" if self._current_folder else name
        try:
            shared_store.add_folder(rel)
        except Exception as e:
            QMessageBox.warning(self, t("common.error"), t("db.op_error", err=str(e)))
            return
        self.refresh()
        self._select_folder(rel)

    def _upload(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            t("db.upload"),
            "",
            "Documents (*.pdf *.docx *.pptx *.html *.txt *.md *.csv);;All Files (*.*)",
        )
        if paths:
            self._ingest_uploaded(paths, self._current_folder)

    def _ingest_uploaded(self, src_paths: list[str], folder: str):
        """Copy local file(s) into the shared KB, then feed them to the vector DB."""
        client = getattr(self.mw, "rag_client", None)
        if not client or not getattr(client, "is_alive", lambda: False)():
            QMessageBox.warning(self, t("common.warning"), t("common.service_offline"))
            return
        dests: list[str] = []
        for p in src_paths:
            dest = shared_store.copy_file_into_kb(p, folder or "")
            if dest:
                dests.append(dest)
                try:
                    shared_store.append_upload(os.path.basename(dest), folder or "")
                except Exception:
                    pass
        if not dests:
            QMessageBox.warning(self, t("common.warning"), t("db.upload_none"))
            return
        self._set_uploading(True)
        self._upload_worker = IngestWorker(client, "files", json.dumps(dests))
        self._upload_worker.done.connect(lambda r, n=len(dests): self._on_upload_done(r, n))
        self._upload_worker.error.connect(self._on_upload_error)
        self._upload_worker.start()

    def _set_uploading(self, busy: bool):
        self.upload_btn.setEnabled(not busy)
        self.upload_btn.setText(t("db.uploading") if busy else t("db.upload"))

    def _on_upload_done(self, r: dict, n: int):
        self._set_uploading(False)
        chunks = 0
        skipped = 0
        if isinstance(r, dict):
            chunks = int(r.get("chunks") or 0)
            errs = r.get("errors") or []
            skipped = len(errs) if isinstance(errs, list) else 0
        msg = t("db.upload_done", n=n, chunks=chunks)
        if skipped:
            msg += "\n" + t("db.upload_skipped", n=skipped)
        QMessageBox.information(self, t("common.ok"), msg)
        self.refresh()

    def _on_upload_error(self, e: str):
        self._set_uploading(False)
        QMessageBox.critical(self, t("db.upload_fail"), str(e))

    def _ingest_kb(self):
        """Recursively feed the whole KB root into the agent."""
        client = getattr(self.mw, "rag_client", None)
        if not client or not getattr(client, "is_alive", lambda: False)():
            QMessageBox.warning(self, t("common.warning"), t("common.service_offline"))
            return
        root = shared_store.kb_root()
        if not root.exists():
            QMessageBox.warning(
                self,
                t("db.ingest_kb_title"),
                t("db.ingest_kb_missing", path=str(root) or "(none)"),
            )
            return
        ans = QMessageBox.question(
            self,
            t("db.ingest_kb_title"),
            t("db.ingest_kb_confirm", path=str(root)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.ingest_kb_btn.setEnabled(False)
        self.ingest_kb_btn.setText(t("db.ingest_kb_running"))
        self._kb_worker = IngestWorker(client, "dir", str(root))
        self._kb_worker.done.connect(self._on_ingest_kb_done)
        self._kb_worker.error.connect(self._on_ingest_kb_error)
        self._kb_worker.start()

    def _on_ingest_kb_done(self, r: dict):
        self.ingest_kb_btn.setEnabled(True)
        self.ingest_kb_btn.setText(t("db.ingest_kb"))
        files = 0
        chunks = 0
        errs: list = []
        if isinstance(r, dict):
            files = int(r.get("files") or 0)
            chunks = int(r.get("chunks") or 0)
            errs = r.get("errors") or []
        msg = t("db.ingest_kb_done", files=files, chunks=chunks)
        if errs:
            msg += "\n\n" + t("db.ingest_kb_errors", n=len(errs) if isinstance(errs, list) else errs)
        QMessageBox.information(self, t("common.ok"), msg)

    def _on_ingest_kb_error(self, e: str):
        self.ingest_kb_btn.setEnabled(True)
        self.ingest_kb_btn.setText(t("db.ingest_kb"))
        QMessageBox.critical(self, t("db.ingest_kb_fail"), str(e))

    # ── Drag & drop ───────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and Path(p).is_file():
                paths.append(p)
        if paths:
            self._ingest_uploaded(paths, self._current_folder)
        event.acceptProposedAction()
