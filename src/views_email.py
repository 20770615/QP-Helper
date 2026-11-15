"""Email fetch UI — recovered from qp_email_helper bytecode (§4.3).

Runs ``_internal/fetch_today.py`` via the bundled ``.venv`` interpreter and
auto-ingests the resulting JSON into the KB through ``RagClient.ingest_emails``.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QDate, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config_manager import APP_ROOT, get_email_account, get_language
from i18n import t
from platform_compat import find_venv_python, subprocess_hidden_kwargs
from rag_client import RagClient

# Portable package root (same role as APP_DIR in the frozen app).
APP_DIR = APP_ROOT


def _app_roots() -> list[Path]:
    """Candidate runtime roots that hold ``.venv`` and ``_internal``.

    When frozen by PyInstaller, ``__file__`` resolves into a temp MEIPASS
    extraction dir, so ``APP_DIR`` alone is NOT where the bundled ``.venv``
    lives. The executable, however, always sits in the real distribution root.
    """
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass).resolve())
    roots.append(APP_DIR.resolve())
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


class IngestWorker(QThread):
    """Background ingestion worker (calls ``RagClient.ingest_emails`` / dir / files)."""

    progress = pyqtSignal(str)
    done = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, client: RagClient, mode: str, path: str):
        super().__init__()
        self.client = client
        self.mode = mode
        self.path = path

    def run(self) -> None:
        try:
            if self.mode == "emails":
                resp = self.client.ingest_emails(self.path)
            elif self.mode == "dir":
                resp = self.client.ingest_directory(self.path)
            else:
                import json

                paths = json.loads(self.path)
                resp = self.client.ingest_files(paths)
            self.done.emit(resp)
        except Exception as e:
            self.error.emit(str(e))


class FetchEmailsWorker(QThread):
    """Runs fetch_today.py to scrape Outlook emails via Playwright."""

    output = pyqtSignal(str)
    done = pyqtSignal(int)
    error = pyqtSignal(str)

    def __init__(self, date: str = "", account: str = ""):
        super().__init__()
        self.date = date
        self.account = account

    def run(self) -> None:
        script = APP_DIR / "fetch_today.py"
        if not script.exists():
            script = APP_DIR / "_internal" / "fetch_today.py"
        if not script.exists():
            self.error.emit("fetch_today.py not found")
            return

        found = find_venv_python(_app_roots(), skip_self=getattr(sys, "frozen", False))
        python: str | None = str(found) if found else None
        if not python and not getattr(sys, "frozen", False):
            python = sys.executable

        if not python:
            self.error.emit(
                "Python interpreter not found. Run scripts/setup_macos.sh (Mac) "
                "or setup.bat (Windows) to create .venv, or install Python and "
                "Playwright to use email fetching."
            )
            return

        cmd = [python, str(script)]
        if self.date:
            cmd += ["--date", self.date]
        if self.account:
            cmd += ["--account", self.account]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **subprocess_hidden_kwargs(),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self.output.emit(line.strip())
            proc.wait()
            self.done.emit(proc.returncode or 0)
        except Exception as e:
            self.error.emit(str(e))


class EmailFetchView(QWidget):
    """Fetch emails — Playwright scraper into reports/today_emails.json (§4.3)."""

    def __init__(self, main_window: "MainWindow"):  # noqa: F821
        super().__init__()
        self.mw = main_window
        self._login_dlg_shown = False
        self._worker: FetchEmailsWorker | None = None
        self._ingest_worker: IngestWorker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        info = QFrame()
        info.setObjectName("card")
        il = QVBoxLayout(info)
        il.setContentsMargins(16, 14, 16, 14)
        il.addWidget(QLabel("📧 " + t("header.emails")))
        self.info_desc = QLabel(t("em.info"))
        self.info_desc.setStyleSheet("font-size: 12px; color: #555;")
        self.info_desc.setWordWrap(True)
        il.addWidget(self.info_desc)
        layout.addWidget(info)

        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(8)
        self.date_lbl = QLabel(t("em.date"))
        self.date_lbl.setStyleSheet("color: #888; font-size: 12px;")
        cfg_row.addWidget(self.date_lbl)

        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("yyyy-MM-dd")
        self.date_input.setDate(QDate.currentDate().addDays(-1))
        self.date_input.setMaximumWidth(160)
        cfg_row.addWidget(self.date_input)
        cfg_row.addStretch()

        self.fetch_btn = QPushButton(t("em.start"))
        self.fetch_btn.setObjectName("primaryBtn")
        self.fetch_btn.clicked.connect(self._fetch)
        cfg_row.addWidget(self.fetch_btn)

        self.reset_btn = QPushButton(t("em.reset"))
        self.reset_btn.clicked.connect(self._reset_view)
        cfg_row.addWidget(self.reset_btn)
        layout.addLayout(cfg_row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        log_card = QFrame()
        log_card.setObjectName("card")
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(16, 14, 16, 14)
        self.log_title_lbl = QLabel(t("em.output"))
        ll.addWidget(self.log_title_lbl)
        self.log_output = QTextBrowser()
        self.log_output.setStyleSheet(
            "background: #f8f8f8; color: #1a1a1a; border-radius: 6px; "
            "font-family: Menlo, Monaco, Consolas, monospace; font-size: 12px;"
        )
        ll.addWidget(self.log_output)
        layout.addWidget(log_card, 1)

    def retranslate(self) -> None:
        self.info_desc.setText(t("em.info"))
        self.date_lbl.setText(t("em.date"))
        self.fetch_btn.setText(t("em.start"))
        self.reset_btn.setText(t("em.reset"))
        self.log_title_lbl.setText(t("em.output"))

    def _profile_path(self, account: str) -> Path:
        """Resolve the Edge user-data dir for this account (mirrors fetch_today)."""
        san = re.sub(
            r"[^a-z0-9._@-]",
            "_",
            (account or "").strip().lower(),
        )
        if san:
            return APP_ROOT / ".edge_profiles" / san
        return APP_ROOT / ".edge_profile"

    def _has_saved_login(self, account: str) -> bool:
        """True if a persisted Outlook session (Cookies) already exists.

        Edge/Chromium stores cookies under Default/Network/Cookies (modern
        layout) or Default/Cookies; older Playwright builds used the profile
        root. Check all known locations.
        """
        p = self._profile_path(account)
        candidates = [
            p / "Default" / "Network" / "Cookies",
            p / "Default" / "Cookies",
            p / "Network" / "Cookies",
            p / "Cookies",
        ]
        return any(c.exists() for c in candidates)

    def _resolve_account(self) -> str:
        """Pick the Outlook account to use, with no UI input box.

        Priority:
          1. The account saved in config (set on first successful login).
          2. A single existing saved profile dir under .edge_profiles/ — reuse
             it so we don't force a re-login after the user already signed in.
          3. Empty -> the shared .edge_profile (login will be captured there).
        """
        acct = (get_email_account() or "").strip()
        if acct:
            return acct
        prof_root = APP_ROOT / ".edge_profiles"
        if prof_root.is_dir():
            subs = [p for p in prof_root.iterdir() if p.is_dir()]
            if len(subs) == 1:
                return subs[0].name
        return ""

    def _fetch(self) -> None:
        self.fetch_btn.setEnabled(False)
        self.reset_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.log_output.clear()
        self._login_dlg_shown = False

        account = self._resolve_account()
        date = self.date_input.date().toString("yyyy-MM-dd")

        if not self._has_saved_login(account):
            QMessageBox.information(
                self,
                t("em.login_title"),
                t("em.login_first"),
            )

        self._worker = FetchEmailsWorker(date=date, account=account)
        self._worker.output.connect(self._on_output)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_output(self, line: str) -> None:
        self.log_output.append(line)
        if self._login_dlg_shown:
            return
        low = line.lower()
        if "需要登录" in line or "请登录" in line or "login" in low:
            self._login_dlg_shown = True
            QMessageBox.information(
                self,
                t("em.login_title"),
                t("em.login_window_open"),
            )

    def _on_done(self, code: int) -> None:
        self.fetch_btn.setEnabled(True)
        self.progress.setVisible(False)
        if code == 0:
            self.log_output.append("\n✅ " + t("em.fetch_done"))
            self._auto_ingest_after_fetch()
            self.log_output.append("\nℹ️ " + t("em.ready_again"))
        else:
            self.log_output.append("\n❌ " + t("em.fetch_fail", code=code))
        self.reset_btn.setEnabled(True)

    def _reset_view(self) -> None:
        """退出当前抓取状态：清空日志、隐藏进度、恢复按钮，便于换日期再抓。"""
        self.log_output.clear()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)
        self.fetch_btn.setEnabled(True)
        self.log_output.append(t("em.info"))

    def _on_error(self, err: str) -> None:
        self.fetch_btn.setEnabled(True)
        self.reset_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.log_output.append(f"\n❌ Error: {err}")

    def _auto_ingest_after_fetch(self) -> None:
        """抓取成功后自动把 today_emails.json 并入知识库。"""
        reports_dir = APP_ROOT / "reports"
        json_file = reports_dir / "today_emails.json"
        if not json_file.exists():
            date = self.date_input.text().strip()
            if date:
                json_file = reports_dir / f"emails_{date}.json"
        if not json_file.exists():
            self.log_output.append("\n⚠ " + t("em.not_found"))
            return
        if not getattr(self.mw, "rag_client", None):
            self.log_output.append("\n⚠ " + t("common.service_offline"))
            return
        self.log_output.append("\n❌ " + t("em.ingesting_auto"))
        worker = IngestWorker(self.mw.rag_client, "emails", str(json_file))
        self._ingest_worker = worker
        worker.done.connect(
            lambda r: self.log_output.append(
                "\n✅ " + t("em.ingested", n=r.get("chunks", 0))
            )
        )
        worker.error.connect(
            lambda e: self.log_output.append(f"\n❌ Ingestion failed:\n{e}")
        )
        worker.start()
