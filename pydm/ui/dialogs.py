"""Dialogs: new download, batch, settings, pairing, confirmation."""
import os
import re
import shutil
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QStackedWidget, QTimeEdit, QVBoxLayout, QWidget)

from ..core import security
from ..core.engine import DEFAULT_SETTINGS, parse_headers, probe_url
from ..core import media, notify, remote, updater
from ..core.util import (category_for, describe_error, expand_pattern, human_size,
                         is_http_url)


def is_download_url(u):
    return is_http_url(u) or (u or "").strip().lower().startswith(("ftp://", "ftps://"))


def qr_pixmap(text, size=180):
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    mat = qr.get_matrix()
    n = len(mat)
    pm = QPixmap(size, size)
    pm.fill(QColor("white"))
    p = QPainter(pm)
    cell = size / n
    for y, row in enumerate(mat):
        for x, on in enumerate(row):
            if on:
                p.fillRect(int(x * cell), int(y * cell), int(cell + 1), int(cell + 1),
                           QColor("black"))
    p.end()
    return pm


DAY_PRESETS = [("Every day", list(range(7))), ("Weekdays", [0, 1, 2, 3, 4]),
               ("Weekends", [5, 6])] + [(n, [i]) for i, n in enumerate(
                   ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])]
from .theme import C, LEVEL_COLORS, icon, pixmap
from .widgets import Banner, Toggle, tile_pixmap

HASH_LEN = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}


def field_label(text):
    lab = QLabel(text)
    lab.setObjectName("FieldLabel")
    return lab


def hint(text):
    lab = QLabel(text)
    lab.setObjectName("Faint")
    lab.setWordWrap(True)
    return lab


def button(text, kind=None, icon_name=None, color=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if icon_name:
        b.setIcon(icon(icon_name, color or ("white" if kind == "Primary" else C["text"]), 16))
    b.setCursor(Qt.PointingHandCursor)
    return b


class Dialog(QDialog):
    def __init__(self, parent, title, subtitle="", icon_name=None, icon_color=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 22, 24, 20)
        self.root.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        if icon_name:
            ic = QLabel()
            ic.setFixedSize(40, 40)
            ic.setAlignment(Qt.AlignCenter)
            col = icon_color or C["accent"]
            ic.setPixmap(pixmap(icon_name, col, 20))
            from PySide6.QtGui import QColor
            q = QColor(col)
            ic.setStyleSheet(f"background: rgba({q.red()},{q.green()},{q.blue()},0.15);"
                             f" border-radius: 11px;")
            head.addWidget(ic)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("Title")
        t.setStyleSheet("font-size: 18px;")
        col.addWidget(t)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("Muted")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        col.addWidget(self.subtitle)
        head.addLayout(col, 1)
        self.root.addLayout(head)

    def add_buttons(self, *buttons):
        row = QHBoxLayout()
        row.setContentsMargins(0, 6, 0, 0)
        row.addStretch()
        for b in buttons:
            row.addWidget(b)
        self.root.addLayout(row)


# --------------------------------------------------------------------------- confirm
class ConfirmDialog(Dialog):
    def __init__(self, parent, title, text, level="caution", findings=None, ok_text="Continue",
                 cancel_text="Cancel", danger=False, checkbox=None):
        icon_name = {"danger": "shield-alert", "caution": "alert"}.get(level, "check-circle")
        super().__init__(parent, title, text, icon_name, LEVEL_COLORS.get(level, C["info"]))
        self.setMinimumWidth(500)
        for f in findings or []:
            self.root.addWidget(Banner(f["level"], f["text"]))
        self.check = None
        if checkbox:
            self.check = Toggle(checkbox)
            self.root.addWidget(self.check)
        cancel = button(cancel_text, "Ghost")
        cancel.clicked.connect(self.reject)
        ok = button(ok_text, "Danger" if danger else "Primary")
        ok.clicked.connect(self.accept)
        (cancel if danger else ok).setDefault(True)
        self.add_buttons(cancel, ok)

    @classmethod
    def ask(cls, parent, *a, **kw):
        dlg = cls(parent, *a, **kw)
        ok = dlg.exec() == QDialog.Accepted
        return (ok, dlg.check.isChecked()) if dlg.check else ok


class PairingDialog(Dialog):
    def __init__(self, parent, origin):
        super().__init__(parent, "Allow browser extension?",
                         "An extension wants permission to send downloads to PyDM.",
                         "globe", C["accent2"])
        self.setMinimumWidth(500)
        box = QLineEdit(origin)
        box.setReadOnly(True)
        self.root.addWidget(field_label("EXTENSION ID"))
        self.root.addWidget(box)
        self.root.addWidget(Banner("caution", "Only allow this if you just installed the PyDM "
                                              "extension yourself. You can revoke access any "
                                              "time in Settings → Browser."))
        deny = button("Deny", "Ghost")
        deny.clicked.connect(self.reject)
        allow = button("Allow", "Primary", "check")
        allow.clicked.connect(self.accept)
        self.add_buttons(deny, allow)


# --------------------------------------------------------------------------- new download
class _Probe(QObject):
    done = Signal(int, object)


class NewDownloadDialog(Dialog):
    def __init__(self, parent, manager, url="", referrer=None, cookies=None, filename=None,
                 source="manual", origin=None):
        sub = "Paste a link — PyDM checks it before downloading."
        if source == "browser":
            sub = "Sent from your browser" + (f" · {origin}" if origin else "")
        elif source == "clipboard":
            sub = "Link detected on your clipboard."
        super().__init__(parent, "New download", sub, "download")
        self.m = manager
        self.referrer, self.cookies, self.source = referrer, cookies, source
        self.info = None
        self.findings = []
        self._token = 0
        self.setMinimumWidth(640)
        s = manager.settings

        self.root.addWidget(field_label("ADDRESS"))
        row = QHBoxLayout()
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("https://example.com/file.zip")
        self.url.setClearButtonEnabled(True)
        row.addWidget(self.url, 1)
        paste = button("Paste", None, "copy")
        paste.clicked.connect(lambda: self.url.setText(QApplication.clipboard().text().strip()))
        row.addWidget(paste)
        self.root.addLayout(row)

        card = QFrame()
        card.setObjectName("Panel")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(14)
        self.tile = QLabel()
        self.tile.setFixedSize(44, 44)
        cl.addWidget(self.tile)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.info_name = QLabel("—")
        self.info_name.setStyleSheet("font-weight: 650; font-size: 14px;")
        self.info_meta = QLabel("Enter a link to check it")
        self.info_meta.setObjectName("Muted")
        col.addWidget(self.info_name)
        col.addWidget(self.info_meta)
        cl.addLayout(col, 1)
        self.root.addWidget(card)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(field_label("FILE NAME"), 0, 0)
        self.name = QLineEdit(filename or "")
        self.name.setPlaceholderText("Detected automatically")
        grid.addWidget(self.name, 1, 0, 1, 3)
        grid.addWidget(field_label("SAVE TO"), 2, 0)
        self.auto = Toggle("Sort into a category folder automatically")
        self.auto.setChecked(True)
        grid.addWidget(self.auto, 3, 0, 1, 3)
        self.folder = QLineEdit(s["download_dir"])
        grid.addWidget(self.folder, 4, 0, 1, 2)
        browse = button("Browse", None, "folder")
        browse.clicked.connect(self._browse)
        grid.addWidget(browse, 4, 2)
        grid.setColumnStretch(0, 1)
        self.root.addLayout(grid)
        self.auto.toggled.connect(lambda on: (self.folder.setEnabled(not on),
                                              browse.setEnabled(not on)))
        self.folder.setEnabled(False)
        browse.setEnabled(False)

        self.adv_btn = button("Advanced options", "Ghost", "settings", C["muted"])
        self.adv_btn.setCheckable(True)
        self.root.addWidget(self.adv_btn, 0, Qt.AlignLeft)
        self.adv = QWidget()
        ag = QGridLayout(self.adv)
        ag.setContentsMargins(0, 0, 0, 0)
        ag.setHorizontalSpacing(12)
        ag.setVerticalSpacing(8)
        ag.addWidget(field_label("CONNECTIONS"), 0, 0)
        ag.addWidget(field_label("SPEED LIMIT (KB/s, 0 = none)"), 0, 1)
        self.conns = QComboBox()
        self.conns.addItem(f"Auto — up to {s['max_connections']} (fastest)", 0)
        for n in (1, 2, 4, 8, 12, 16, 24, 32, 48, 64):
            self.conns.addItem(f"{n} connection{'s' if n > 1 else ''}", n)
        if not s["auto_connections"]:
            idx = self.conns.findData(int(s["connections"]))
            self.conns.setCurrentIndex(max(0, idx))
        self.limit = QSpinBox()
        self.limit.setRange(0, 10_000_000)
        self.limit.setSingleStep(256)
        ag.addWidget(self.conns, 1, 0)
        ag.addWidget(self.limit, 1, 1)
        ag.addWidget(field_label("VERIFY CHECKSUM"), 2, 0, 1, 2)
        crow = QHBoxLayout()
        self.algo = QComboBox()
        self.algo.addItems(["None", "SHA-256", "SHA-512", "SHA-1", "MD5"])
        self.hash = QLineEdit()
        self.hash.setPlaceholderText("Paste the checksum published by the website")
        crow.addWidget(self.algo)
        crow.addWidget(self.hash, 1)
        ag.addLayout(crow, 3, 0, 1, 2)
        ag.addWidget(field_label("REFERRER"), 4, 0, 1, 2)
        self.ref = QLineEdit(referrer or "")
        ag.addWidget(self.ref, 5, 0, 1, 2)
        ag.addWidget(field_label("CUSTOM HEADERS (one per line, Name: value)"), 6, 0, 1, 2)
        self.headers = QPlainTextEdit()
        self.headers.setFixedHeight(64)
        self.headers.setPlaceholderText("Authorization: Bearer …")
        ag.addWidget(self.headers, 7, 0, 1, 2)
        ag.addWidget(hint("Authorization and Cookie headers are kept in memory only and never "
                          "saved to disk."), 8, 0, 1, 2)
        ag.addWidget(field_label("MIRRORS — same file on other servers (one per line)"), 9, 0, 1, 2)
        self.mirrors = QPlainTextEdit()
        self.mirrors.setFixedHeight(56)
        self.mirrors.setPlaceholderText("https://mirror2.example.com/file.zip")
        ag.addWidget(self.mirrors, 10, 0, 1, 2)
        ag.addWidget(hint("PyDM downloads from all mirrors at once. Each mirror is checked to "
                          "have the same file size; add a checksum to prove they're "
                          "identical."), 11, 0, 1, 2)
        self.stream = Toggle("Streaming order — download from the start so media can be "
                             "previewed early")
        ag.addWidget(self.stream, 12, 0, 1, 2)
        self.extract = Toggle("Extract the archive when finished (safely)")
        self.extract.setChecked(bool(s["extract_archives"]))
        ag.addWidget(self.extract, 13, 0, 1, 2)
        self.adv.setObjectName("Clear")
        self.adv_scroll = QScrollArea()
        self.adv_scroll.setWidgetResizable(True)
        self.adv_scroll.setWidget(self.adv)
        self.adv_scroll.viewport().setObjectName("Clear")
        self.adv_scroll.setFixedHeight(250)
        self.adv_scroll.setVisible(False)
        self.root.addWidget(self.adv_scroll)
        self.adv_btn.toggled.connect(self._toggle_adv)

        self.warn_box = QVBoxLayout()
        self.warn_box.setSpacing(8)
        self.root.addLayout(self.warn_box)

        cancel = button("Cancel", "Ghost")
        cancel.clicked.connect(self.reject)
        self.want_video = False
        self.video_btn = button("Video options", None, "film")
        self.video_btn.clicked.connect(self._to_video)
        later = button("Add to queue", None, "clock")
        later.clicked.connect(lambda: self._submit(False))
        self.start_btn = button("Start download", "Primary", "download")
        self.start_btn.clicked.connect(lambda: self._submit(True))
        self.start_btn.setDefault(True)
        self.add_buttons(cancel, self.video_btn, later, self.start_btn)

        self.probe = _Probe()
        self.probe.done.connect(self._probed)
        self.timer = QTimer(self, singleShot=True, interval=500)
        self.timer.timeout.connect(self._start_probe)
        self.url.textChanged.connect(lambda: self.timer.start())
        self.name.textChanged.connect(self._refresh_warnings)
        self._refresh_tile("Other")
        if url:
            self.timer.start(50)
        else:
            self.url.setFocus()

    def _to_video(self):
        from .extra import VideoDialog
        url = self.url.text().strip()
        self.reject()
        VideoDialog(self.parent(), self.m, url, cookies=self.cookies,
                    referrer=self.referrer, source=self.source).exec()

    def _toggle_adv(self, on):
        self.adv_scroll.setVisible(on)
        self.adjustSize()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def _refresh_tile(self, category):
        self.tile.setPixmap(tile_pixmap(category, 44))

    def _start_probe(self):
        url = self.url.text().strip()
        self._token += 1
        token = self._token
        self.info = None
        if not is_download_url(url):
            self.info_name.setText("—")
            self.info_meta.setText("Enter an http(s):// or ftp:// link" if url else
                                   "Enter a link to check it")
            self.findings = []
            self._refresh_warnings()
            return
        self.info_meta.setText("Checking link…")
        headers = {"User-Agent": self.m.settings["user_agent"], "Accept-Encoding": "identity"}
        if self.referrer:
            headers["Referer"] = self.referrer
        if self.cookies:
            headers["Cookie"] = self.cookies
        opener, timeout = self.m.opener, min(15, int(self.m.settings["timeout"]))

        def work():
            try:
                if url.lower().startswith(("ftp://", "ftps://")):
                    from ..core import ftp as ftpmod
                    from ..core.engine import ssl_context
                    info = ftpmod.probe(url, timeout, ssl_context())
                else:
                    info = probe_url(url, headers, opener, timeout)
                info["findings"] = security.pre_download_findings(info["raw_name"],
                                                                  info["final_url"])
            except Exception as e:  # noqa: BLE001
                info = {"error": describe_error(e),
                        "findings": security.pre_download_findings(
                            self.name.text() or url.rsplit("/", 1)[-1], url)}
            self.probe.done.emit(token, info)
        threading.Thread(target=work, daemon=True).start()

    def _probed(self, token, info):
        if token != self._token:
            return
        self.findings = info.get("findings", [])
        if "error" in info:
            self.info_name.setText("Couldn't check this link")
            self.info_meta.setText(info["error"] + " — you can still add it.")
            self.info = None
        else:
            self.info = info
            name = self.name.text().strip() or info["name"]
            self.info_name.setText(name)
            bits = [human_size(info["total"]) if info["total"] is not None else "Unknown size",
                    "Resumable · multi-connection" if info["resumable"] else
                    "Single connection · can't resume"]
            if info["final_url"].startswith("https://"):
                bits.append("Secure connection")
            self.info_meta.setText("   ·   ".join(bits))
            self._refresh_tile(category_for(name))
        self._refresh_warnings()

    def _refresh_warnings(self):
        while self.warn_box.count():
            w = self.warn_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        items = list(self.findings)
        custom = self.name.text().strip()
        if custom:
            items = [f for f in items if "extension" not in f["text"] and "hidden" not in f["text"]]
            items += security.name_findings(custom)
        url = self.url.text().strip()
        dup = self.m.find_url(url) if url else None
        if dup:
            items.append({"level": "caution",
                          "text": f"This link is already in your list ({dup.status.lower()})."})
        elif url and self.m.history:
            old = self.m.history.find_url(url)
            if old:
                import time as _t
                items.append({"level": "caution", "text": "You already downloaded this on "
                              f"{_t.strftime('%b %d', _t.localtime(old['finished']))}: "
                              f"{old['path']}"})
        from .extra import looks_like_video
        if url and looks_like_video(url):
            self.want_video = True
            items.append({"level": "info", "text": "This looks like a video page or stream — "
                          "click “Video options” to choose the quality."})
        if self.info and self.info["total"]:
            folder = self.folder.text() if not self.auto.isChecked() else self.m.settings["download_dir"]
            try:
                free = shutil.disk_usage(folder if os.path.isdir(folder) else
                                         os.path.expanduser("~")).free
                if free < self.info["total"]:
                    items.append({"level": "danger", "text": "Not enough free disk space."})
            except OSError:
                pass
        for f in items:
            self.warn_box.addWidget(Banner(f["level"], f["text"]))
        self.adjustSize()

    def _submit(self, start):
        url = self.url.text().strip()
        if not is_download_url(url):
            self._error("Please enter a valid http(s):// or ftp:// link.")
            return
        try:
            headers = parse_headers(self.headers.toPlainText())
        except ValueError as e:
            self._error(str(e))
            return
        checksum = None
        if self.algo.currentIndex() > 0:
            algo = self.algo.currentText().lower().replace("-", "")
            value = re.sub(r"\s", "", self.hash.text()).lower()
            if not re.fullmatch(r"[0-9a-f]+", value or "x") or len(value) != HASH_LEN[algo]:
                self._error(f"A {self.algo.currentText()} checksum is {HASH_LEN[algo]} "
                            "hexadecimal characters.")
                return
            checksum = (algo, value)
        risky = [f for f in self.findings if f["level"] == "danger"]
        if risky and not ConfirmDialog.ask(
                self, "Download anyway?", "PyDM found serious warning signs for this file.",
                "danger", risky, "Download anyway", "Go back", danger=True):
            return
        mirrors = [u for u in self.mirrors.toPlainText().split() if u.strip()]
        bad = [u for u in mirrors if not is_download_url(u)]
        if bad:
            self._error(f"Not a valid mirror link: {bad[0]}")
            return
        name = self.name.text().strip() or None
        ref = self.ref.text().strip() or None
        n = self.conns.currentData()
        self.m.add(url, save_dir=None if self.auto.isChecked() else self.folder.text().strip(),
                   filename=name, referrer=ref if is_http_url(ref or "") else None,
                   cookies=self.cookies, start=start, source=self.source, checksum=checksum,
                   speed_limit_kbps=self.limit.value(), headers=headers,
                   connections=n or None, auto_tune=(n == 0), mirrors=mirrors,
                   sequential=self.stream.isChecked(), extract=self.extract.isChecked())
        self.accept()

    def _error(self, text):
        self.findings = [f for f in self.findings if not f.get("_err")]
        self.findings.append({"level": "danger", "text": text, "_err": True})
        self._refresh_warnings()


# --------------------------------------------------------------------------- batch
class BatchDialog(Dialog):
    def __init__(self, parent, manager, text=""):
        super().__init__(parent, "Batch download",
                         "Paste many links at once, or use a pattern like "
                         "https://site.com/photo[001-150].jpg", "layers")
        self.m = manager
        self.setMinimumWidth(640)
        self.edit = QPlainTextEdit(text)
        self.edit.setPlaceholderText("https://example.com/part1.zip\nhttps://example.com/"
                                     "part2.zip\nhttps://example.com/episode[01-12].mp4")
        self.edit.setMinimumHeight(200)
        self.root.addWidget(self.edit)
        self.count = QLabel()
        self.count.setObjectName("Muted")
        self.root.addWidget(self.count)
        self.auto = Toggle("Sort into category folders automatically")
        self.auto.setChecked(True)
        self.root.addWidget(self.auto)
        self.start = Toggle("Start downloading right away")
        self.start.setChecked(True)
        self.root.addWidget(self.start)
        cancel = button("Cancel", "Ghost")
        cancel.clicked.connect(self.reject)
        self.ok = button("Add downloads", "Primary", "plus")
        self.ok.clicked.connect(self._submit)
        self.add_buttons(cancel, self.ok)
        self.edit.textChanged.connect(self._update)
        self._update()

    def urls(self):
        out, seen = [], set()
        for line in self.edit.toPlainText().split():
            for u in expand_pattern(line.strip()):
                if is_download_url(u) and u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    def _update(self):
        urls = self.urls()
        dups = sum(1 for u in urls if self.m.find_url(u))
        self.count.setText(f"{len(urls)} link{'s' if len(urls) != 1 else ''} found"
                           + (f" · {dups} already in your list (skipped)" if dups else ""))
        self.ok.setEnabled(len(urls) - dups > 0)
        self.ok.setText(f"Add {len(urls) - dups} downloads" if len(urls) - dups > 0
                        else "Add downloads")

    def _submit(self):
        for u in self.urls():
            if not self.m.find_url(u):
                self.m.add(u, save_dir=None if self.auto.isChecked()
                           else self.m.settings["download_dir"],
                           start=self.start.isChecked(), source="batch")
        self.accept()


# --------------------------------------------------------------------------- settings
class SettingsDialog(QDialog):
    PAGES = [("General", "grid"), ("Speed", "zap"), ("Bandwidth", "gauge"),
             ("Network", "globe"), ("Automation", "refresh"), ("Security", "shield"),
             ("Video", "film"), ("Scheduler", "calendar"), ("Browser", "link"),
             ("Remote & phone", "activity"), ("Updates", "download")]

    def __init__(self, parent, manager, bridge=None, bridge_error=None):
        super().__init__(parent)
        self.m, self.bridge = manager, bridge
        self.setWindowTitle("Settings")
        self.resize(920, 700)
        s = manager.settings
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(190)
        for name, ic in self.PAGES:
            self.nav.addItem(QListWidgetItem(icon(ic, C["muted"], 16), name))
        outer.addWidget(self.nav)
        right = QVBoxLayout()
        right.setContentsMargins(28, 24, 28, 20)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        btns = QHBoxLayout()
        btns.addStretch()
        cancel = button("Cancel", "Ghost")
        cancel.clicked.connect(self.reject)
        save = button("Save changes", "Primary", "check")
        save.clicked.connect(self._save)
        btns.addWidget(cancel)
        btns.addWidget(save)
        right.addLayout(btns)
        outer.addLayout(right, 1)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        # General
        p, lay = self._page("General", "Where files go and how the app behaves.")
        lay.addWidget(field_label("DOWNLOAD FOLDER"))
        row = QHBoxLayout()
        self.dir = QLineEdit(s["download_dir"])
        b = button("Browse", None, "folder")
        b.clicked.connect(lambda: self._pick(self.dir))
        row.addWidget(self.dir, 1)
        row.addWidget(b)
        lay.addLayout(row)
        self.cats = self._toggle(lay, "Sort files into category folders (Video, Music, …)",
                                 s["use_categories"])
        lay.addWidget(field_label("SIMULTANEOUS DOWNLOADS"))
        self.active = self._spin(1, 10, s["max_active"])
        lay.addWidget(self.active, 0, Qt.AlignLeft)
        self.tray = self._toggle(lay, "Keep running in the system tray when the window is "
                                      "closed", s["close_to_tray"])
        self.notify = self._toggle(lay, "Show notifications", s["notifications"])
        self.clip = self._toggle(lay, "Detect download links copied to the clipboard",
                                 s["monitor_clipboard"])
        self.hist = self._toggle(lay, "Keep download history and statistics",
                                 s["history_enabled"])
        lay.addStretch()

        # Speed
        p, lay = self._page("Speed", "How PyDM squeezes the most out of your connection.")
        self.auto = self._toggle(lay, "Auto-tune connections (recommended)",
                                 s["auto_connections"])
        lay.addWidget(hint("Starts with 8 connections and keeps doubling while the speed keeps "
                           "rising. Backs off and remembers the limit when a server pushes back."))
        g = QGridLayout()
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(8)
        g.addWidget(field_label("MAX CONNECTIONS PER DOWNLOAD"), 0, 0)
        g.addWidget(field_label("FIXED CONNECTIONS (auto off)"), 0, 1)
        g.addWidget(field_label("TOTAL CONNECTION BUDGET"), 0, 2)
        self.maxc = self._spin(1, 64, s["max_connections"])
        self.conns = self._spin(1, 64, s["connections"])
        self.budget = self._spin(8, 512, s["max_total_connections"], 8)
        g.addWidget(self.maxc, 1, 0)
        g.addWidget(self.conns, 1, 1)
        g.addWidget(self.budget, 1, 2)
        lay.addLayout(g)
        self.auto.toggled.connect(lambda on: self.conns.setEnabled(not on))
        self.conns.setEnabled(not s["auto_connections"])
        self.slow = self._toggle(lay, "Replace slow connections automatically",
                                 s["replace_slow_connections"])
        lay.addWidget(hint("A connection running far below the others is re-dialled — it often "
                           "lands on a faster server node. Free connections also take over work "
                           "from slow ones near the end of a download."))
        lay.addWidget(field_label("GLOBAL SPEED LIMIT (KB/s, 0 = unlimited)"))
        self.limit = self._spin(0, 10_000_000, s["speed_limit_kbps"], 256)
        lay.addWidget(self.limit, 0, Qt.AlignLeft)
        learned = s.get("host_limits") or {}
        if learned:
            lay.addWidget(field_label("LEARNED SERVER LIMITS"))
            lay.addWidget(hint(", ".join(f"{h}: {n}" for h, n in list(learned.items())[:8])))
            self.reset_limits = button("Forget learned limits", "Ghost", "refresh", C["muted"])
            self.reset_limits.setCheckable(True)
            lay.addWidget(self.reset_limits, 0, Qt.AlignLeft)
        else:
            self.reset_limits = None
        lay.addStretch()

        # Bandwidth
        p, lay = self._page("Bandwidth", "Share your connection fairly — automatically.")
        lay.addWidget(field_label("SPEED RULES BY TIME"))
        lay.addWidget(hint("e.g. 500 KB/s on weekdays 09:00–17:00 while you work. The lowest "
                           "active limit wins."))
        self.rules_box = QVBoxLayout()
        self.rules_box.setSpacing(6)
        lay.addLayout(self.rules_box)
        self.rule_rows = []
        for r in s.get("speed_rules", []):
            self._add_rule(r)
        addr = button("Add rule", "Ghost", "plus", C["muted"])
        addr.clicked.connect(lambda: self._add_rule({"days": [0, 1, 2, 3, 4], "start": "09:00",
                                                     "end": "17:00", "kbps": 1024}))
        lay.addWidget(addr, 0, Qt.AlignLeft)
        self.yield_ = self._toggle(lay, "Slow down when other apps need the internet",
                                   s["yield_to_apps"])
        lay.addWidget(hint("PyDM watches your total network traffic and gives way when "
                           "something else (a video call, streaming, a game) starts using it."))
        self.quiet = self._toggle(lay, "Quiet mode while a game, full-screen app or "
                                       "presentation is running", s["quiet_when_fullscreen"])
        lay.addWidget(field_label("QUIET MODE SPEED (KB/s)"))
        self.quiet_kbps = self._spin(16, 1_000_000, s["quiet_kbps"], 128)
        lay.addWidget(self.quiet_kbps, 0, Qt.AlignLeft)
        if not sys.platform.startswith("win"):
            lay.addWidget(hint("Full-screen detection works on Windows."))
        lay.addStretch()

        # Network
        p, lay = self._page("Network", "Proxy and connection behaviour.")
        g = QGridLayout()
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(8)
        g.addWidget(field_label("TIMEOUT (seconds)"), 0, 0)
        g.addWidget(field_label("RETRIES PER CONNECTION"), 0, 1)
        self.timeout = self._spin(5, 300, s["timeout"])
        self.retries = self._spin(0, 50, s["max_retries"])
        g.addWidget(self.timeout, 1, 0)
        g.addWidget(self.retries, 1, 1)
        lay.addLayout(g)
        lay.addWidget(field_label("PROXY"))
        self.proxy = QLineEdit(s["proxy"])
        self.proxy.setPlaceholderText("Empty = system proxy · e.g. http://user:pass@127.0.0.1:8080")
        lay.addWidget(self.proxy)
        lay.addWidget(field_label("USER AGENT"))
        self.ua = QLineEdit(s["user_agent"])
        lay.addWidget(self.ua)
        lay.addStretch()

        # Automation
        p, lay = self._page("Automation", "Recover from problems and act on finished files.")
        self.retry = self._toggle(lay, "Automatically retry failed downloads",
                                  s["auto_retry"])
        lay.addWidget(hint("Network drops, timeouts and server errors are retried with growing "
                           "waits (10 s → 5 min). Your progress is always kept."))
        lay.addWidget(field_label("MAXIMUM AUTOMATIC RETRIES"))
        self.retry_limit = self._spin(1, 100, s["auto_retry_limit"])
        lay.addWidget(self.retry_limit, 0, Qt.AlignLeft)
        self.nosleep = self._toggle(lay, "Keep the computer awake while downloading",
                                    s["prevent_sleep"])
        self.extract_def = self._toggle(lay, "Extract archives when finished (zip, tar; 7z/rar "
                                             "with 7-Zip)", s["extract_archives"])
        self.del_archive = self._toggle(lay, "Delete the archive after extracting",
                                        s["delete_archive_after_extract"])
        lay.addWidget(hint("Extraction refuses unsafe archives: paths that escape the folder, "
                           "symlinks and zip bombs. Files flagged as threats are never "
                           "extracted."))
        lay.addWidget(field_label("RUN A COMMAND AFTER EACH DOWNLOAD (optional)"))
        self.post = QLineEdit(s["post_command"])
        self.post.setPlaceholderText('e.g.  "C:\\Tools\\import.exe" "{path}"')
        lay.addWidget(self.post)
        lay.addWidget(hint("Placeholders: {path} {folder} {name} {url}. Runs directly, never "
                           "through a shell, and never for files flagged as threats."))
        lay.addStretch()

        # Security
        p, lay = self._page("Security", "Protections applied before and after every download.")
        lay.addWidget(Banner("safe", "TLS certificate and hostname verification is always on "
                                     "(TLS 1.2+). Connections with invalid certificates are "
                                     "refused."))
        self.downgrade = self._toggle(lay, "Block redirects from HTTPS to insecure HTTP",
                                      s["block_downgrade_redirects"])
        self.insecure = self._toggle(lay, "Block programs (.exe, .msi, …) served over insecure "
                                          "HTTP", s["block_insecure_executables"])
        self.motw = self._toggle(lay, "Mark downloads as coming from the Internet "
                                      "(Windows SmartScreen protection)", s["mark_of_the_web"])
        if not sys.platform.startswith("win"):
            self.motw.setEnabled(False)
            lay.addWidget(hint("Mark-of-the-Web is a Windows feature."))
        self.av = self._toggle(lay, "Scan finished downloads with antivirus", s["antivirus_scan"])
        lay.addWidget(hint(self._av_name()))
        self.risky = self._toggle(lay, "Warn before opening programs or flagged files",
                                  s["confirm_risky_open"])
        self.autosum = self._toggle(lay, "Find and verify checksums the website publishes "
                                         "(SHA256SUMS, .sha256 …)", s["auto_checksum"])
        lay.addWidget(field_label("VIRUSTOTAL API KEY (optional)"))
        row = QHBoxLayout()
        self.vt = QLineEdit(s["virustotal_api_key"])
        self.vt.setEchoMode(QLineEdit.Password)
        self.vt.setPlaceholderText("Free key from virustotal.com")
        show = button("Show", "Ghost")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.vt.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        row.addWidget(self.vt, 1)
        row.addWidget(show)
        lay.addLayout(row)
        lay.addWidget(hint("Only the file's SHA-256 fingerprint is sent to VirusTotal — never "
                           "the file itself. The key is stored in your PyDM settings file, "
                           "readable only by your user account."))
        lay.addStretch()

        # Video
        p, lay = self._page("Video", "Tools PyDM uses for video sites and streams.")
        try:
            import yt_dlp
            ytv = yt_dlp.version.__version__
            lay.addWidget(Banner("safe", f"yt-dlp {ytv} — YouTube and 1,800+ video sites "
                                         "supported."))
        except ImportError:
            lay.addWidget(Banner("caution", "yt-dlp isn't installed — video sites won't work. "
                                            "Install it with:  pip install -U yt-dlp"))
        ff = media.ffmpeg_path(s)
        lay.addWidget(Banner("safe" if ff else "caution",
                             f"ffmpeg found: {ff}" if ff else
                             "ffmpeg not found — HD videos (separate video + audio), MP3 and "
                             "encrypted streams need it. Get it from ffmpeg.org."))
        lay.addWidget(field_label("FFMPEG LOCATION (optional)"))
        row = QHBoxLayout()
        self.ffmpeg = QLineEdit(s["ffmpeg_path"])
        self.ffmpeg.setPlaceholderText("Auto-detect")
        fb = button("Browse", None, "folder")
        fb.clicked.connect(lambda: self._pick_file(self.ffmpeg))
        row.addWidget(self.ffmpeg, 1)
        row.addWidget(fb)
        lay.addLayout(row)
        lay.addWidget(hint("DRM-protected streams (Netflix, Disney+, etc.) are never "
                           "downloaded."))
        lay.addStretch()

        # Scheduler
        p, lay = self._page("Scheduler", "Download at night or off-peak hours automatically.")
        self.sched = self._toggle(lay, "Enable scheduler", s["scheduler_enabled"])
        g = QGridLayout()
        g.setHorizontalSpacing(16)
        g.addWidget(field_label("START QUEUE AT"), 0, 0)
        g.addWidget(field_label("PAUSE QUEUE AT"), 0, 1)
        self.t_start = QTimeEdit(QTime.fromString(s["scheduler_start"], "HH:mm"))
        self.t_stop = QTimeEdit(QTime.fromString(s["scheduler_stop"], "HH:mm"))
        for t in (self.t_start, self.t_stop):
            t.setDisplayFormat("HH:mm")
        g.addWidget(self.t_start, 1, 0)
        g.addWidget(self.t_stop, 1, 1)
        lay.addLayout(g)
        self.window_only = self._toggle(lay, "Only download inside this time window",
                                        s["scheduler_window_only"])
        lay.addWidget(hint("When on, new queued downloads wait until the start time."))
        lay.addWidget(field_label("WHEN THE QUEUE FINISHES"))
        self.queue_done = QComboBox()
        self.queue_done.addItems(["Do nothing", "Quit PyDM"])
        self.queue_done.setCurrentIndex(1 if s["on_queue_done"] == "quit" else 0)
        lay.addWidget(self.queue_done)
        lay.addStretch()

        # Browser
        p, lay = self._page("Browser", "Catch downloads from Chrome, Edge and Brave.")
        status = (f"Listening on 127.0.0.1:{s['browser_port']} — only approved extensions can "
                  "add downloads." if bridge and not bridge_error else
                  f"Browser integration is off: {bridge_error or 'not started'}")
        lay.addWidget(Banner("safe" if bridge and not bridge_error else "caution", status))
        lay.addWidget(field_label("PORT (restart PyDM after changing)"))
        self.port = self._spin(1024, 65535, s["browser_port"])
        lay.addWidget(self.port)
        self.ask = self._toggle(lay, "Ask before starting downloads sent from the browser",
                                s["confirm_browser_downloads"])
        lay.addWidget(field_label("APPROVED EXTENSIONS"))
        self.trusted = QListWidget()
        self.trusted.setObjectName("Trusted")
        self.trusted.setFixedHeight(100)
        for o in s["trusted_origins"]:
            self.trusted.addItem(o)
        lay.addWidget(self.trusted)
        rm = button("Revoke selected", "Danger", "x", "#fda4af")
        rm.clicked.connect(lambda: [self.trusted.takeItem(self.trusted.row(i))
                                    for i in self.trusted.selectedItems()])
        lay.addWidget(rm, 0, Qt.AlignLeft)
        lay.addWidget(hint("Install: open chrome://extensions → enable Developer mode → Load "
                           "unpacked → choose the 'extension' folder that came with PyDM. The "
                           "first time it sends a download, PyDM asks you to approve it."))
        lay.addStretch()

        # Remote & phone
        p, lay = self._page("Remote & phone", "Control PyDM from your phone and get notified.")
        self.remote_on = self._toggle(lay, "Enable remote control web app", s["remote_enabled"])
        self.remote_lan = self._toggle(lay, "Allow phones on my Wi-Fi (otherwise this PC only)",
                                       s["remote_lan"])
        g = QGridLayout()
        g.addWidget(field_label("PORT"), 0, 0)
        self.remote_port = self._spin(1024, 65535, s["remote_port"])
        g.addWidget(self.remote_port, 1, 0)
        lay.addLayout(g)
        self.token = s["remote_token"] or remote.new_token()
        qrow = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setFixedSize(180, 180)
        qrow.addWidget(self.qr)
        qcol = QVBoxLayout()
        self.remote_url = QLineEdit()
        self.remote_url.setReadOnly(True)
        qcol.addWidget(hint("Scan with your phone's camera (same Wi-Fi), or open the link:"))
        qcol.addWidget(self.remote_url)
        brow = QHBoxLayout()
        cp = button("Copy link", None, "copy")
        cp.clicked.connect(lambda: QApplication.clipboard().setText(self.remote_url.text()))
        regen = button("New secret link", "Ghost", "refresh", C["muted"])
        regen.clicked.connect(self._regen)
        brow.addWidget(cp)
        brow.addWidget(regen)
        brow.addStretch()
        qcol.addLayout(brow)
        qcol.addWidget(hint("The link contains a secret key. Anyone who has it can control "
                            "PyDM — make a new one if it leaks. Use only on Wi-Fi you trust."))
        qcol.addStretch()
        qrow.addLayout(qcol, 1)
        lay.addLayout(qrow)
        for w in (self.remote_lan, self.remote_port):
            (w.toggled if hasattr(w, "toggled") else w.valueChanged).connect(self._refresh_qr)
        self._refresh_qr()
        lay.addSpacing(8)
        self.ntfy_on = self._toggle(lay, "Send phone notifications (ntfy app — free)",
                                    s["ntfy_enabled"])
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.addWidget(field_label("NTFY SERVER"), 0, 0)
        g.addWidget(field_label("PRIVATE TOPIC"), 0, 1)
        self.ntfy_server = QLineEdit(s["ntfy_server"])
        self.ntfy_topic = QLineEdit(s["ntfy_topic"] or notify.new_topic())
        g.addWidget(self.ntfy_server, 1, 0)
        g.addWidget(self.ntfy_topic, 1, 1)
        lay.addLayout(g)
        trow = QHBoxLayout()
        test = button("Send test notification", None, "zap")
        test.clicked.connect(self._test_ntfy)
        self.ntfy_state = QLabel("")
        self.ntfy_state.setObjectName("Faint")
        trow.addWidget(test)
        trow.addWidget(self.ntfy_state, 1)
        lay.addLayout(trow)
        lay.addWidget(hint("Install “ntfy” on your phone and subscribe to the topic above. The "
                           "topic name is your secret — keep it private."))
        lay.addStretch()

        # Updates
        p, lay = self._page("Updates", "Signed, verified updates.")
        from .. import release
        self.upd_on = self._toggle(lay, "Check for updates when PyDM starts", s["check_updates"])
        lay.addWidget(field_label("UPDATE MANIFEST URL"))
        self.upd_url = QLineEdit(s["update_url"] or release.UPDATE_URL)
        self.upd_url.setPlaceholderText("https://…/manifest.json")
        lay.addWidget(self.upd_url)
        lay.addWidget(field_label("RELEASE PUBLIC KEY (Ed25519)"))
        self.upd_key = QLineEdit(s["update_public_key"] or release.PUBLIC_KEY)
        lay.addWidget(self.upd_key)
        urow = QHBoxLayout()
        chk = button("Check now", None, "refresh")
        chk.clicked.connect(self._check_update)
        self.upd_state = QLabel("")
        self.upd_state.setObjectName("Faint")
        self.upd_state.setWordWrap(True)
        urow.addWidget(chk)
        urow.addWidget(self.upd_state, 1)
        lay.addLayout(urow)
        lay.addWidget(hint("Updates are only offered when the manifest's signature matches the "
                           "release key, and the installer must match the signed SHA-256. "
                           "Use tools/sign_release.py to publish your own builds."))
        lay.addStretch()
        self.nav.setCurrentRow(0)

    @staticmethod
    def _av_name():
        if sys.platform.startswith("win"):
            return ("Uses Microsoft Defender." if security._find_defender()
                    else "Microsoft Defender was not found on this PC.")
        if shutil.which("clamdscan") or shutil.which("clamscan"):
            return "Uses ClamAV."
        return "No scanner found — install ClamAV to enable scanning on this system."

    def _page(self, title, subtitle):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("Title")
        lay.addWidget(t)
        st = QLabel(subtitle)
        st.setObjectName("Muted")
        lay.addWidget(st)
        lay.addSpacing(6)
        scroll.setWidget(page)
        self.stack.addWidget(scroll)
        return page, lay

    @staticmethod
    def _toggle(lay, text, value):
        t = Toggle(text)
        t.setChecked(bool(value))
        lay.addWidget(t)
        return t

    @staticmethod
    def _spin(lo, hi, value, step=1):
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setValue(int(value))
        return sp

    def _add_rule(self, r):
        row = QWidget()
        row.setObjectName("Clear")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        days = QComboBox()
        for label, _ds in DAY_PRESETS:
            days.addItem(label)
        idx = next((i for i, (_l, ds) in enumerate(DAY_PRESETS)
                    if ds == sorted(r.get("days", []))), 0)
        days.setCurrentIndex(idx)
        start = QTimeEdit(QTime.fromString(r.get("start", "09:00"), "HH:mm"))
        end = QTimeEdit(QTime.fromString(r.get("end", "17:00"), "HH:mm"))
        for t in (start, end):
            t.setDisplayFormat("HH:mm")
        kbps = self._spin(16, 10_000_000, r.get("kbps", 1024), 128)
        kbps.setSuffix(" KB/s")
        rm = QPushButton()
        rm.setObjectName("Icon")
        rm.setIcon(icon("x", C["muted"], 14))
        for w in (days, QLabel("from"), start, QLabel("to"), end, kbps):
            h.addWidget(w)
        h.addWidget(rm)
        rec = (row, days, start, end, kbps)
        rm.clicked.connect(lambda: (self.rule_rows.remove(rec), row.deleteLater()))
        self.rule_rows.append(rec)
        self.rules_box.addWidget(row)

    def _rules(self):
        return [{"days": DAY_PRESETS[d.currentIndex()][1], "start": a.time().toString("HH:mm"),
                 "end": b.time().toString("HH:mm"), "kbps": k.value(), "enabled": True}
                for _r, d, a, b, k in self.rule_rows]

    def _refresh_qr(self, *_):
        host = remote.lan_ip() if self.remote_lan.isChecked() else "127.0.0.1"
        url = f"http://{host}:{self.remote_port.value()}/#t={self.token}"
        self.remote_url.setText(url)
        pm = qr_pixmap(url)
        if pm:
            self.qr.setPixmap(pm)
        else:
            self.qr.setText("Install 'qrcode'\nfor a QR code")

    def _regen(self):
        self.token = remote.new_token()
        self._refresh_qr()

    def _test_ntfy(self):
        ok = notify.send(self.ntfy_server.text().strip(), self.ntfy_topic.text().strip(),
                         "PyDM test", "Notifications are working ✓")
        self.ntfy_state.setText("Sent — check your phone." if ok else
                                "Couldn't send (check the server/topic and your connection).")

    def _check_update(self):
        try:
            man = updater.check(self.upd_url.text().strip(), self.upd_key.text().strip(),
                                self.m.opener)
            self.upd_state.setText(f"Version {man['version']} is available." if man
                                   else "You're up to date.")
        except updater.UpdateError as e:
            self.upd_state.setText(str(e))
        except Exception as e:  # noqa: BLE001
            self.upd_state.setText(f"Couldn't check: {describe_error(e)}")

    def _pick_file(self, edit):
        f, _ = QFileDialog.getOpenFileName(self, "Choose ffmpeg", edit.text())
        if f:
            edit.setText(f)

    def _pick(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
        if d:
            edit.setText(d)

    def _save(self):
        trusted = [self.trusted.item(i).text() for i in range(self.trusted.count())]
        removed = set(self.m.settings["trusted_origins"]) - set(trusted)
        self.m.update_settings({
            "download_dir": self.dir.text().strip() or DEFAULT_SETTINGS["download_dir"],
            "use_categories": self.cats.isChecked(), "max_active": self.active.value(),
            "connections": self.conns.value(), "close_to_tray": self.tray.isChecked(),
            "auto_connections": self.auto.isChecked(), "max_connections": self.maxc.value(),
            "max_total_connections": self.budget.value(),
            "replace_slow_connections": self.slow.isChecked(),
            "auto_retry": self.retry.isChecked(), "auto_retry_limit": self.retry_limit.value(),
            "prevent_sleep": self.nosleep.isChecked(),
            "extract_archives": self.extract_def.isChecked(),
            "delete_archive_after_extract": self.del_archive.isChecked(),
            "post_command": self.post.text().strip(),
            "history_enabled": self.hist.isChecked(), "auto_checksum": self.autosum.isChecked(),
            "speed_rules": self._rules(), "yield_to_apps": self.yield_.isChecked(),
            "quiet_when_fullscreen": self.quiet.isChecked(),
            "quiet_kbps": self.quiet_kbps.value(), "ffmpeg_path": self.ffmpeg.text().strip(),
            "remote_enabled": self.remote_on.isChecked(), "remote_lan": self.remote_lan.isChecked(),
            "remote_port": self.remote_port.value(), "remote_token": self.token,
            "ntfy_enabled": self.ntfy_on.isChecked(),
            "ntfy_server": self.ntfy_server.text().strip() or "https://ntfy.sh",
            "ntfy_topic": self.ntfy_topic.text().strip(),
            "check_updates": self.upd_on.isChecked(), "update_url": self.upd_url.text().strip(),
            "update_public_key": self.upd_key.text().strip(),
            "notifications": self.notify.isChecked(), "monitor_clipboard": self.clip.isChecked(),
            "speed_limit_kbps": self.limit.value(), "timeout": self.timeout.value(),
            "max_retries": self.retries.value(), "proxy": self.proxy.text().strip(),
            "user_agent": self.ua.text().strip() or DEFAULT_SETTINGS["user_agent"],
            "block_downgrade_redirects": self.downgrade.isChecked(),
            "block_insecure_executables": self.insecure.isChecked(),
            "mark_of_the_web": self.motw.isChecked(), "antivirus_scan": self.av.isChecked(),
            "confirm_risky_open": self.risky.isChecked(),
            "virustotal_api_key": self.vt.text().strip(),
            "scheduler_enabled": self.sched.isChecked(),
            "scheduler_start": self.t_start.time().toString("HH:mm"),
            "scheduler_stop": self.t_stop.time().toString("HH:mm"),
            "scheduler_window_only": self.window_only.isChecked(),
            "on_queue_done": "quit" if self.queue_done.currentIndex() == 1 else "nothing",
            "browser_port": self.port.value(),
            "confirm_browser_downloads": self.ask.isChecked(), "trusted_origins": trusted,
        })
        if self.reset_limits is not None and self.reset_limits.isChecked():
            self.m.update_settings({"host_limits": {}})
        if self.bridge and removed:
            with self.bridge._lock:
                self.bridge._denied.update(removed)
        self.accept()
