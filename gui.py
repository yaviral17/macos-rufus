#!/usr/bin/env python3
"""macos-rufus GUI — a PySide6 wizard on top of rufus.py's existing logic.

This never mounts/formats/writes disks itself: all destructive disk work
happens in rufus_worker.py, launched as root via the native macOS
authorization dialog (osascript), while this app stays an unprivileged,
double-clickable .app. Downloading and ISO selection also reuse rufus.py's
functions directly — nothing here reimplements Microsoft API handling,
resumable downloads, or disk formatting; it only adds an interactive
front-end and background threads around them.
"""

import json
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QRadioButton, QStackedWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

import rufus


def fmt_size(n: int) -> str:
    return rufus.fmt_size(n)


def _pick_download_option_gui(links: list[dict], previous_name: str | None = None) -> dict:
    """Same intent as rufus._pick_download_option, but never falls back to a
    blocking terminal prompt (there's no TTY in a GUI app) — when there's a
    genuine choice and no prior selection to match, default to 64-bit, since
    that's what the overwhelming majority of users need."""
    if len(links) == 1:
        return links[0]
    if previous_name:
        match = next((l for l in links if l.get("Name") == previous_name), None)
        if match:
            return match
    x64 = next((l for l in links if rufus._extract_arch(l["Uri"]) == "x64"), None)
    return x64 or links[0]


# ── background workers ───────────────────────────────────────────────────────

class FetchOptionsWorker(QThread):
    """Looks up editions + languages for a dynamic (Win 10/11) OS entry."""
    ready = Signal(list, list, object, str)  # editions, skus, session, edition_id
    error = Signal(str)

    def __init__(self, entry: dict):
        super().__init__()
        self.entry = entry

    def run(self):
        try:
            session = rufus._ms_download_session(self.entry["slug"])
            editions = rufus.fetch_product_editions(session, self.entry["slug"])
            edition_id, _ = editions[0]
            skus = rufus.fetch_skus(session, edition_id, str(uuid.uuid4()))
            self.ready.emit(editions, skus, session, edition_id)
        except Exception as e:
            self.error.emit(str(e))


class DownloadWorker(QThread):
    """Drives a fresh download or a resume, reusing rufus.py's download
    machinery (Playwright link-fetch, resumable HTTP download) unchanged."""
    status = Signal(str)
    progress = Signal(int, int)  # downloaded, total
    finished_ok = Signal(str)    # dest path
    error = Signal(str)
    paused = Signal(str)         # dest path of the paused .iso.part

    def __init__(self, mode: str, **kwargs):
        super().__init__()
        self.mode = mode  # "fresh" or "resume"
        self.kwargs = kwargs
        self._pause_event = threading.Event()
        self._dest = None

    def request_pause(self):
        self._pause_event.set()

    def run(self):
        try:
            if self.mode == "fresh":
                self._run_fresh()
            else:
                self._run_resume()
        except KeyboardInterrupt:
            self.paused.emit(str(self._dest) if self._dest else "")
        except Exception as e:
            self.error.emit(str(e))

    def _run_fresh(self):
        entry = self.kwargs["entry"]
        downloads_dir = self.kwargs["downloads_dir"]
        session = self.kwargs["session"]
        edition_id = self.kwargs["edition_id"]
        sku = self.kwargs["sku"]

        self.status.emit("Opening a headless browser to get past Microsoft's bot-check...")
        rufus._ensure_playwright_ready()
        links = rufus.fetch_download_links(entry["slug"], edition_id, sku["Id"], sku["Language"])
        chosen = _pick_download_option_gui(links)
        url = chosen["Uri"]

        arch = rufus._extract_arch(url)
        lang = sku.get("Language", "en-us")
        filename = f"{entry['name'].replace(' ', '').replace('.', '')}_{lang}_{arch}.iso"
        dest = downloads_dir / filename
        self._dest = dest

        state = {
            "entry_name": entry["name"], "slug": entry["slug"], "edition_id": edition_id,
            "sku_id": sku["Id"], "sku_language": sku.get("LocalizedLanguage"),
            "sku_language_raw": sku["Language"], "option_name": chosen.get("Name"),
            "url": url, "dest": str(dest), "created": datetime.now().isoformat(),
        }
        rufus._write_download_state(dest.with_suffix(".iso.part"), state)

        self.status.emit(f"Downloading {dest.name}...")
        rufus.download_iso_with_progress(
            session, url, dest,
            progress_cb=lambda d, t: self.progress.emit(d, t),
            should_pause=self._pause_event.is_set,
        )
        self.finished_ok.emit(str(dest))

    def _run_resume(self):
        item = self.kwargs["item"]
        state, part_path = item["state"], item["part_path"]
        dest = Path(state["dest"])
        self._dest = dest
        session = rufus._ms_download_session(state["slug"])

        self.status.emit(f"Resuming {dest.name}...")
        try:
            rufus.download_iso_with_progress(
                session, state["url"], dest, resume=True,
                progress_cb=lambda d, t: self.progress.emit(d, t),
                should_pause=self._pause_event.is_set,
            )
        except rufus.requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in (400, 401, 403, 404, 410):
                raise
            self.status.emit("Download link expired — requesting a fresh one from Microsoft...")
            rufus._ensure_playwright_ready()
            links = rufus.fetch_download_links(
                state["slug"], state["edition_id"], state["sku_id"],
                state.get("sku_language_raw", state.get("sku_language")),
            )
            chosen = _pick_download_option_gui(links, state.get("option_name"))
            state["url"] = chosen["Uri"]
            rufus._write_download_state(part_path, state)
            self.status.emit(f"Resuming {dest.name}...")
            rufus.download_iso_with_progress(
                session, state["url"], dest, resume=True,
                progress_cb=lambda d, t: self.progress.emit(d, t),
                should_pause=self._pause_event.is_set,
            )
        self.finished_ok.emit(str(dest))


class FlashWorker(QThread):
    """Launches rufus_worker.py as root via the native macOS auth dialog,
    backgrounded so the dialog returns immediately, then polls its
    JSON-lines progress file to report status back to the GUI thread."""
    stage = Signal(dict)
    finished_ok = Signal(dict)
    error = Signal(str)

    def __init__(self, iso_path: Path, disk_node: str):
        super().__init__()
        self.iso_path = iso_path
        self.disk_node = disk_node

    def run(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="macos-rufus-"))
        args_path = tmp_dir / "args.json"
        progress_path = tmp_dir / "progress.jsonl"
        worker_log = tmp_dir / "worker.log"
        args_path.write_text(json.dumps({"iso_path": str(self.iso_path), "disk_node": self.disk_node}))
        progress_path.write_text("")

        worker_script = Path(__file__).resolve().parent / "rufus_worker.py"
        inner_cmd = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(worker_script))} "
            f"{shlex.quote(str(args_path))} {shlex.quote(str(progress_path))} "
            f"> {shlex.quote(str(worker_log))} 2>&1 &"
        )
        osa_escaped = inner_cmd.replace("\\", "\\\\").replace('"', '\\"')
        osa_src = f'do shell script "{osa_escaped}" with administrator privileges'

        try:
            subprocess.run(["osascript", "-e", osa_src], check=True,
                            capture_output=True, text=True, timeout=120)
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or str(e)).strip()
            if "User canceled" in msg or "-128" in msg:
                self.error.emit("Authorization was cancelled.")
            else:
                self.error.emit(f"Failed to start with administrator privileges: {msg}")
            return
        except subprocess.TimeoutExpired:
            self.error.emit("Timed out waiting for administrator authorization.")
            return

        last_pos = 0
        # The backgrounded worker needs a moment to actually start writing;
        # keep polling until it reports a terminal stage.
        while True:
            time.sleep(0.3)
            if not progress_path.exists():
                continue
            text = progress_path.read_text()
            if len(text) <= last_pos:
                continue
            new_lines = text[last_pos:].splitlines()
            last_pos = len(text)
            for line in new_lines:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.stage.emit(data)
                if data.get("stage") == "complete":
                    self.finished_ok.emit(data)
                    return
                if data.get("stage") == "error":
                    self.error.emit(data.get("message", "Unknown error"))
                    return


# ── UI pages ──────────────────────────────────────────────────────────────────

class OsSelectPage(QWidget):
    def __init__(self, on_next):
        super().__init__()
        self.on_next = on_next
        layout = QVBoxLayout(self)
        title = QLabel("Select an Operating System")
        title.setFont(QFont("", 18, QFont.Bold))
        layout.addWidget(title)
        layout.addWidget(QLabel("Windows 10/11 can be downloaded automatically. "
                                 "Other versions need an existing ISO file."))

        self.list = QListWidget()
        for entry in rufus.OS_CATALOG:
            tag = "Auto-download available" if entry["dynamic"] else "Manual ISO only"
            item = QListWidgetItem(f"{entry['name']}  —  {tag}")
            item.setData(Qt.UserRole, entry)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        layout.addWidget(self.list)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        next_btn = QPushButton("Next →")
        next_btn.clicked.connect(self._next)
        btn_row.addWidget(next_btn)
        layout.addLayout(btn_row)

    def _next(self):
        item = self.list.currentItem()
        if item:
            self.on_next(item.data(Qt.UserRole))


class IsoAcquirePage(QWidget):
    """Handles both the manual-path flow and the dynamic download flow."""

    def __init__(self, on_ready, on_start_download, on_back):
        super().__init__()
        self.on_ready = on_ready
        self.on_start_download = on_start_download
        self.on_back = on_back
        self.entry = None
        self.session = None
        self.edition_id = None
        self.skus = []

        self.layout_ = QVBoxLayout(self)
        self.title = QLabel("")
        self.title.setFont(QFont("", 18, QFont.Bold))
        self.layout_.addWidget(self.title)

        self.status_label = QLabel("")
        self.layout_.addWidget(self.status_label)

        # -- manual path widgets --
        self.manual_box = QGroupBox("ISO file")
        mv = QVBoxLayout(self.manual_box)
        self.path_label = QLabel("No file selected")
        browse_btn = QPushButton("Browse for .iso file...")
        browse_btn.clicked.connect(self._browse)
        mv.addWidget(self.path_label)
        mv.addWidget(browse_btn)
        self.layout_.addWidget(self.manual_box)

        # -- existing downloads widgets --
        self.existing_box = QGroupBox("Already downloaded")
        ev = QVBoxLayout(self.existing_box)
        self.existing_list = QListWidget()
        ev.addWidget(self.existing_list)
        use_existing_btn = QPushButton("Use selected file")
        use_existing_btn.clicked.connect(self._use_existing)
        ev.addWidget(use_existing_btn)
        self.layout_.addWidget(self.existing_box)

        # -- dynamic download widgets --
        self.download_box = QGroupBox("Download from Microsoft")
        dv = QVBoxLayout(self.download_box)
        self.edition_combo = QComboBox()
        self.language_combo = QComboBox()
        dv.addWidget(QLabel("Edition:"))
        dv.addWidget(self.edition_combo)
        dv.addWidget(QLabel("Language:"))
        dv.addWidget(self.language_combo)
        download_btn = QPushButton("Download")
        download_btn.clicked.connect(self._start_download)
        dv.addWidget(download_btn)
        self.layout_.addWidget(self.download_box)

        self.selected_path = None

        btn_row = QHBoxLayout()
        back_btn = QPushButton("← Back")
        back_btn.clicked.connect(self.on_back)
        btn_row.addWidget(back_btn)
        btn_row.addStretch()
        self.next_btn = QPushButton("Next →")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(self._next)
        btn_row.addWidget(self.next_btn)
        self.layout_.addLayout(btn_row)

    def load(self, entry: dict, downloads_dir: Path):
        self.entry = entry
        self.selected_path = None
        self.next_btn.setEnabled(False)
        self.title.setText(entry["name"])
        self.downloads_dir = downloads_dir

        if not entry["dynamic"]:
            self.manual_box.setVisible(True)
            self.existing_box.setVisible(False)
            self.download_box.setVisible(False)
            self.path_label.setText("No file selected")
            if entry["name"] != "I already have an ISO file":
                self.status_label.setText(
                    f"{entry['name']} isn't available as a direct Microsoft download anymore. "
                    "Please download it manually and select the file below."
                )
            else:
                self.status_label.setText("")
            return

        self.manual_box.setVisible(True)
        self.existing_box.setVisible(False)
        self.download_box.setVisible(False)
        self.status_label.setText("Contacting Microsoft for download options...")

        found = rufus.find_existing_isos(entry, downloads_dir)
        if found:
            self.existing_box.setVisible(True)
            self.existing_list.clear()
            for p in found:
                stat = p.stat()
                label = f"{p.name}  ({fmt_size(stat.st_size)}, {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M})"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, p)
                self.existing_list.addItem(item)

        self._fetch_worker = FetchOptionsWorker(entry)
        self._fetch_worker.ready.connect(self._on_options_ready)
        self._fetch_worker.error.connect(self._on_options_error)
        self._fetch_worker.start()

    def _on_options_ready(self, editions, skus, session, edition_id):
        self.session = session
        self.edition_id = edition_id
        self.skus = skus
        self.status_label.setText("Pick an edition/language, then download — or use an existing file above.")
        self.download_box.setVisible(True)

        self.edition_combo.clear()
        for eid, name in editions:
            self.edition_combo.addItem(name, eid)
        idx = self.edition_combo.findData(edition_id)
        if idx >= 0:
            self.edition_combo.setCurrentIndex(idx)

        self.language_combo.clear()
        for sku in skus:
            self.language_combo.addItem(sku.get("LocalizedLanguage", sku.get("Language", "?")), sku)
        eng_idx = next((i for i, s in enumerate(skus) if s.get("Language") == "English"), 0)
        self.language_combo.setCurrentIndex(eng_idx)

    def _on_options_error(self, message):
        self.status_label.setText(f"Couldn't reach Microsoft: {message}\nUse Browse to select an ISO manually.")

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Windows ISO", str(Path.home()), "ISO files (*.iso)")
        if path:
            self.selected_path = Path(path)
            self.path_label.setText(str(self.selected_path))
            self.next_btn.setEnabled(True)

    def _use_existing(self):
        item = self.existing_list.currentItem()
        if item:
            self.selected_path = item.data(Qt.UserRole)
            self.path_label.setText(f"Using existing: {self.selected_path}")
            self.next_btn.setEnabled(True)

    def _start_download(self):
        edition_id = self.edition_combo.currentData()
        sku = self.language_combo.currentData()
        if not sku:
            return
        self.on_start_download(
            entry=self.entry, downloads_dir=self.downloads_dir, session=self.session,
            edition_id=edition_id, sku=sku,
        )

    def _next(self):
        if self.selected_path:
            self.on_ready(self.selected_path)


class DownloadPage(QWidget):
    def __init__(self, on_done, on_choose_manually):
        super().__init__()
        self.on_done = on_done
        self.on_choose_manually = on_choose_manually
        self.worker = None
        self._last_bytes = 0
        self._last_time = None

        layout = QVBoxLayout(self)
        title = QLabel("Downloading")
        title.setFont(QFont("", 18, QFont.Bold))
        layout.addWidget(title)

        self.status_label = QLabel("Starting...")
        layout.addWidget(self.status_label)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        layout.addWidget(self.bar)

        self.detail_label = QLabel("")
        layout.addWidget(self.detail_label)

        btn_row = QHBoxLayout()
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self._pause)
        btn_row.addWidget(self.pause_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def start(self, mode: str, **kwargs):
        self.bar.setValue(0)
        self.detail_label.setText("")
        self.status_label.setText("Starting...")
        self._last_bytes = 0
        self._last_time = time.monotonic()
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Pause")

        self.worker = DownloadWorker(mode, **kwargs)
        self.worker.status.connect(self.status_label.setText)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.paused.connect(self._on_paused)
        self.worker.start()

    def _pause(self):
        if self.worker:
            self.pause_btn.setEnabled(False)
            self.status_label.setText("Pausing...")
            self.worker.request_pause()

    def _on_progress(self, downloaded, total):
        now = time.monotonic()
        dt = now - self._last_time
        if dt > 0.3:
            speed = (downloaded - self._last_bytes) / dt
            self._last_bytes = downloaded
            self._last_time = now
            eta = (total - downloaded) / speed if speed > 0 and total else None
            eta_str = f", ETA {int(eta)}s" if eta is not None else ""
            self.detail_label.setText(
                f"{fmt_size(downloaded)} / {fmt_size(total) if total else '?'} "
                f"({fmt_size(speed)}/s{eta_str})"
            )
        if total:
            self.bar.setValue(int(downloaded * 100 / total))

    def _on_finished(self, dest):
        self.status_label.setText(f"Downloaded {Path(dest).name}")
        self.bar.setValue(100)
        self.on_done(Path(dest))

    def _on_error(self, message):
        QMessageBox.critical(self, "Download failed", message)
        self.on_choose_manually()

    def _on_paused(self, dest_part):
        self.status_label.setText("Paused — progress saved. You can resume later from the OS selection screen.")
        self.pause_btn.setEnabled(False)


class UsbSelectPage(QWidget):
    def __init__(self, on_next, on_back):
        super().__init__()
        self.on_next = on_next
        self.on_back = on_back
        layout = QVBoxLayout(self)
        title = QLabel("Select a USB Drive")
        title.setFont(QFont("", 18, QFont.Bold))
        layout.addWidget(title)
        layout.addWidget(QLabel("Only external, physical drives are listed — internal disks are never shown."))

        self.list = QListWidget()
        layout.addWidget(self.list)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn)

        btn_row = QHBoxLayout()
        back_btn = QPushButton("← Back")
        back_btn.clicked.connect(self.on_back)
        btn_row.addWidget(back_btn)
        btn_row.addStretch()
        self.next_btn = QPushButton("Next →")
        self.next_btn.clicked.connect(self._next)
        btn_row.addWidget(self.next_btn)
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        self.list.clear()
        drives = rufus.list_usb_drives()
        for d in drives:
            label = f"{d['node']}  —  {d['name']}  ({fmt_size(d['size'])}, {d['protocol']})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, d)
            self.list.addItem(item)
        if drives:
            self.list.setCurrentRow(0)

    def _next(self):
        item = self.list.currentItem()
        if item:
            self.on_next(item.data(Qt.UserRole))
        else:
            QMessageBox.warning(self, "No drive selected", "Plug in a USB drive and click Refresh.")


class ConfirmPage(QWidget):
    def __init__(self, on_confirm, on_back):
        super().__init__()
        self.on_confirm = on_confirm
        self.on_back = on_back
        layout = QVBoxLayout(self)
        title = QLabel("Confirm")
        title.setFont(QFont("", 18, QFont.Bold))
        layout.addWidget(title)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #b00020; font-weight: bold;")
        layout.addWidget(self.warning_label)

        self.summary_label = QLabel("")
        layout.addWidget(self.summary_label)

        self.checkbox = QCheckBox("I understand this drive will be completely erased")
        self.checkbox.stateChanged.connect(self._on_checked)
        layout.addWidget(self.checkbox)

        btn_row = QHBoxLayout()
        back_btn = QPushButton("← Back")
        back_btn.clicked.connect(self.on_back)
        btn_row.addWidget(back_btn)
        btn_row.addStretch()
        self.flash_btn = QPushButton("Flash Drive")
        self.flash_btn.setEnabled(False)
        self.flash_btn.clicked.connect(self.on_confirm)
        btn_row.addWidget(self.flash_btn)
        layout.addLayout(btn_row)

    def load(self, iso_path: Path, drive: dict):
        self.warning_label.setText(
            f"{drive['node']} ({drive['name']}, {fmt_size(drive['size'])}) will be COMPLETELY ERASED."
        )
        self.summary_label.setText(f"ISO: {iso_path.name}")
        self.checkbox.setChecked(False)

    def _on_checked(self, state):
        self.flash_btn.setEnabled(bool(state))


class FlashPage(QWidget):
    STAGES = [
        ("mount", "Mount ISO"),
        ("format", "Erase & format drive"),
        ("bootsectors", "Write boot sectors"),
        ("copy_files", "Copy boot files"),
        ("copy_wim", "Copy Windows image"),
        ("eject", "Flush & eject"),
    ]

    def __init__(self, on_done, on_error_back):
        super().__init__()
        self.on_done = on_done
        self.on_error_back = on_error_back
        self.worker = None

        layout = QVBoxLayout(self)
        title = QLabel("Flashing USB Drive")
        title.setFont(QFont("", 18, QFont.Bold))
        layout.addWidget(title)
        layout.addWidget(QLabel("This needs administrator privileges — macOS will ask for your password."))

        self.stage_labels = {}
        for key, label in self.STAGES:
            lbl = QLabel(f"○  {label}")
            layout.addWidget(lbl)
            self.stage_labels[key] = lbl

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        layout.addWidget(self.bar)

        self.detail_label = QLabel("")
        layout.addWidget(self.detail_label)

    def start(self, iso_path: Path, disk_node: str):
        for key, label in self.STAGES:
            self.stage_labels[key].setText(f"○  {label}")
        self.bar.setValue(0)
        self.detail_label.setText("Waiting for administrator authorization...")

        self.worker = FlashWorker(iso_path, disk_node)
        self.worker.stage.connect(self._on_stage)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_stage(self, data: dict):
        stage = data.get("stage")
        status = data.get("status")
        label = self.stage_labels.get(stage)
        if label:
            base = dict(self.STAGES)[stage]
            if status == "start":
                label.setText(f"●  {base}...")
            elif status == "done":
                label.setText(f"✓  {base}")
            elif status == "skipped":
                label.setText(f"—  {base} (not needed)")

        if stage == "copy_files" and status == "progress":
            done, total = data.get("done", 0), data.get("total", 1)
            self.bar.setValue(int(done * 100 / total) if total else 0)
            self.detail_label.setText(f"{done}/{total} files — {data.get('filename', '')}")
        elif stage == "copy_wim" and status == "progress":
            done, total = data.get("done", 0), data.get("total", 1)
            self.bar.setValue(int(done * 100 / total) if total else 0)
            self.detail_label.setText(f"{fmt_size(done)} / {fmt_size(total)}")
        elif status == "start":
            self.detail_label.setText(f"{dict(self.STAGES).get(stage, stage)}...")

    def _on_finished(self, data: dict):
        self.bar.setValue(100)
        self.detail_label.setText("Done!")
        self.on_done(data)

    def _on_error(self, message):
        QMessageBox.critical(self, "Flashing failed", message)
        self.on_error_back()


class DonePage(QWidget):
    def __init__(self, on_restart):
        super().__init__()
        self.on_restart = on_restart
        layout = QVBoxLayout(self)
        title = QLabel("✓ Done!")
        title.setFont(QFont("", 20, QFont.Bold))
        layout.addWidget(title)
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        layout.addWidget(self.summary)
        restart_btn = QPushButton("Flash Another Drive")
        restart_btn.clicked.connect(self.on_restart)
        layout.addWidget(restart_btn)

    def load(self, iso_path: Path, drive: dict, data: dict):
        boot_mode = "UEFI + Legacy BIOS" if data.get("uefi") else "Legacy BIOS only (enable CSM on target PC)"
        elapsed = rufus.fmt_duration(data.get("elapsed", 0))
        self.summary.setPlainText(
            f"ISO: {iso_path.name}\n"
            f"Drive: {drive['node']} ({drive['name']}, {fmt_size(drive['size'])})\n"
            f"Boot mode: {boot_mode}\n"
            f"Time taken: {elapsed}\n"
            f"Log: {data.get('log_path', '')}\n\n"
            "Safe to unplug."
        )


# ── main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("macos-rufus")
        self.resize(640, 520)

        self.iso_path = None
        self.drive = None
        self.downloads_dir = rufus.get_downloads_dir()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.os_page = OsSelectPage(self._on_os_chosen)
        self.iso_page = IsoAcquirePage(self._on_iso_ready, self._on_start_download, self._go_os_page)
        self.download_page = DownloadPage(self._on_download_done, self._go_iso_page_manual)
        self.usb_page = UsbSelectPage(self._on_usb_chosen, self._go_iso_page)
        self.confirm_page = ConfirmPage(self._on_confirmed, self._go_usb_page)
        self.flash_page = FlashPage(self._on_flash_done, self._go_confirm_page)
        self.done_page = DonePage(self._restart)

        for page in (self.os_page, self.iso_page, self.download_page, self.usb_page,
                     self.confirm_page, self.flash_page, self.done_page):
            self.stack.addWidget(page)

        self._check_resumable_downloads()

    def _check_resumable_downloads(self):
        items = rufus.find_incomplete_downloads(self.downloads_dir)
        for item in items:
            state, part_path = item["state"], item["part_path"]
            so_far = part_path.stat().st_size
            label = f"{state.get('entry_name', 'ISO')} ({state.get('sku_language', '?')})"
            reply = QMessageBox.question(
                self, "Resume download?",
                f"Found an incomplete {label} download — {fmt_size(so_far)} saved.\n\nResume it?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.stack.setCurrentWidget(self.download_page)
                self.download_page.start("resume", item=item)
                return
            discard = QMessageBox.question(
                self, "Discard?", "Discard this incomplete download?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if discard == QMessageBox.Yes:
                part_path.unlink(missing_ok=True)
                item["state_path"].unlink(missing_ok=True)

    # -- navigation --
    def _go_os_page(self):
        self.stack.setCurrentWidget(self.os_page)

    def _go_iso_page(self):
        self.stack.setCurrentWidget(self.iso_page)

    def _go_iso_page_manual(self):
        self.stack.setCurrentWidget(self.iso_page)

    def _go_usb_page(self):
        self.stack.setCurrentWidget(self.usb_page)

    def _go_confirm_page(self):
        self.stack.setCurrentWidget(self.confirm_page)

    def _on_os_chosen(self, entry):
        self.iso_page.load(entry, self.downloads_dir)
        self.stack.setCurrentWidget(self.iso_page)

    def _on_start_download(self, **kwargs):
        self.stack.setCurrentWidget(self.download_page)
        self.download_page.start("fresh", **kwargs)

    def _on_download_done(self, path: Path):
        self._on_iso_ready(path)

    def _on_iso_ready(self, path: Path):
        self.iso_path = path
        self.usb_page.refresh()
        self.stack.setCurrentWidget(self.usb_page)

    def _on_usb_chosen(self, drive: dict):
        self.drive = drive
        self.confirm_page.load(self.iso_path, drive)
        self.stack.setCurrentWidget(self.confirm_page)

    def _on_confirmed(self):
        self.stack.setCurrentWidget(self.flash_page)
        self.flash_page.start(self.iso_path, self.drive["node"])

    def _on_flash_done(self, data: dict):
        self.done_page.load(self.iso_path, self.drive, data)
        self.stack.setCurrentWidget(self.done_page)

    def _restart(self):
        self.iso_path = None
        self.drive = None
        self.stack.setCurrentWidget(self.os_page)


def main():
    missing = [t for t in ("hdiutil", "diskutil") if not __import__("shutil").which(t)]
    if missing:
        print(f"Missing required macOS tools: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
