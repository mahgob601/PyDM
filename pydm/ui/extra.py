"""Video downloader, site grabber and statistics view."""
import os
import threading
import time
import urllib.parse

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget, QAbstractItemView)

from ..core import grabber, media
from ..core.util import human_size, is_http_url, reveal_in_folder
from .dialogs import Dialog, button, field_label
from .theme import C, CAT_COLORS, accent_gradient, icon
from .widgets import Banner, Toggle, chip_style

VIDEO_SITES = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv",
               "twitter.com", "x.com", "tiktok.com", "instagram.com", "facebook.com",
               "fb.watch", "reddit.com", "streamable.com", "bilibili.com", "soundcloud.com",
               "rumble.com", "ok.ru", "vk.com")


def looks_like_video(url):
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return (p.path.lower().endswith((".m3u8", ".mpd"))
            or any(host == s or host.endswith("." + s) for s in VIDEO_SITES))


class _Sig(QObject):
    done = Signal(int, object)
    progress = Signal(int, object)


# --------------------------------------------------------------------------- video
class VideoDialog(Dialog):
    """Pick a quality for a video page, an HLS/DASH stream or a direct media file."""

    def __init__(self, parent, manager, page_url="", candidates=None, cookies=None,
                 referrer=None, title=None, source="manual"):
        super().__init__(parent, "Download video", "PyDM finds every quality it can — pick one.",
                         "film", "#f43f5e")
        self.m, self.cookies, self.referrer, self.source = manager, cookies, referrer, source
        self.candidates = list(candidates or [])
        self.page_title = title
        self.options = []
        self._token = 0
        self.setMinimumWidth(640)
        self.root.addWidget(field_label("VIDEO PAGE OR STREAM ADDRESS"))
        row = QHBoxLayout()
        self.url = QLineEdit(page_url)
        self.url.setPlaceholderText("https://www.youtube.com/watch?v=…  or  …/playlist.m3u8")
        row.addWidget(self.url, 1)
        find = button("Find videos", None, "search")
        find.clicked.connect(self.scan)
        row.addWidget(find)
        self.root.addLayout(row)
        self.state = QLabel("")
        self.state.setObjectName("Muted")
        self.state.setWordWrap(True)
        self.root.addWidget(self.state)
        self.list = QListWidget()
        self.list.setObjectName("Trusted")
        self.list.setMinimumHeight(220)
        self.list.itemDoubleClicked.connect(lambda _i: self.submit(True))
        self.root.addWidget(self.list)
        self.root.addWidget(field_label("FILE NAME"))
        self.name = QLineEdit(title or "")
        self.name.setPlaceholderText("Uses the video title")
        self.root.addWidget(self.name)
        self.tools = QVBoxLayout()
        self.root.addLayout(self.tools)
        if not media.ytdlp_available():
            self.tools.addWidget(Banner("caution", "For YouTube and other video sites, install "
                                                   "yt-dlp:  pip install yt-dlp"))
        if not media.ffmpeg_path(manager.settings):
            self.tools.addWidget(Banner("caution", "Install ffmpeg to get HD qualities (video "
                                                   "and audio are separate there) and MP3."))
        cancel = button("Cancel", "Ghost")
        cancel.clicked.connect(self.reject)
        later = button("Add to queue", None, "clock")
        later.clicked.connect(lambda: self.submit(False))
        self.ok = button("Download", "Primary", "download")
        self.ok.clicked.connect(lambda: self.submit(True))
        self.ok.setEnabled(False)
        self.add_buttons(cancel, later, self.ok)
        self.sig = _Sig()
        self.sig.done.connect(self._found)
        if page_url or self.candidates:
            QTimer.singleShot(50, self.scan)

    def scan(self):
        url = self.url.text().strip()
        self._token += 1
        token = self._token
        self.list.clear()
        self.options = []
        self.ok.setEnabled(False)
        if not url and not self.candidates:
            return
        self.state.setText("Looking for videos…")
        cands, cookies, ref, m = list(self.candidates), self.cookies, self.referrer, self.m

        def work():
            found, notes = [], []
            # 1) the site itself, through yt-dlp
            if url and is_http_url(url) and media.ytdlp_available() and \
                    not url.lower().split("?")[0].endswith((".m3u8",)):
                try:
                    info = media.resolve(url, cookies=cookies)
                    have_ff = bool(media.ffmpeg_path(m.settings))
                    for o in media.choices(info, have_ff):
                        found.append({"src": "site", "label": o["label"], "option": o,
                                      "info": info, "size": o.get("size"),
                                      "container": o["container"], "title": info["title"]})
                except Exception as e:  # noqa: BLE001
                    notes.append(str(e))
            # 2) streams / files the browser saw on the page
            all_c = cands + ([{"url": url, "type": "hls"}] if url.lower().split("?")[0]
                             .endswith(".m3u8") else [])
            for c in all_c:
                u, t = c["url"], c.get("type", "")
                if t == "hls" or u.lower().split("?")[0].endswith(".m3u8"):
                    try:
                        st = m.inspect_stream(u, referrer=ref, cookies=cookies)
                        if st["type"] == "master":
                            for o in st["options"]:
                                found.append({"src": "stream", "label": o["label"],
                                              "url": u, "height": o["height"],
                                              "container": "mp4", "size": None,
                                              "extra": "video + audio" if o["audio_url"] else ""})
                        else:
                            found.append({"src": "stream", "label": "Stream", "url": u,
                                          "height": None, "container": "mp4", "size": None})
                    except Exception as e:  # noqa: BLE001
                        notes.append(f"Stream: {e}")
                elif t == "dash" or u.lower().split("?")[0].endswith(".mpd"):
                    notes.append("DASH stream found — use the page address (yt-dlp handles it).")
                elif t in ("video", "audio") or c.get("size"):
                    name = urllib.parse.unquote(urllib.parse.urlsplit(u).path.rsplit("/", 1)[-1])
                    found.append({"src": "file", "label": f"Direct file · {name[:40]}",
                                  "url": u, "size": c.get("size"),
                                  "container": os.path.splitext(name)[1].lstrip(".") or "mp4"})
            self.sig.done.emit(token, (found, notes))
        threading.Thread(target=work, daemon=True).start()

    def _found(self, token, result):
        if token != self._token:
            return
        found, notes = result
        self.options = found
        self.list.clear()
        src_names = {"site": "Website", "stream": "Stream", "file": "File"}
        for i, f in enumerate(found):
            cont = f.get("container", "").upper()
            bits = [f["label"], "" if cont in f["label"].upper() else cont]
            if f.get("size"):
                bits.append(human_size(f["size"]))
            if f.get("extra"):
                bits.append(f["extra"])
            it = QListWidgetItem(icon("film" if f["src"] != "file" else "file", C["muted"], 16),
                                 "   ·   ".join(b for b in bits if b) +
                                 f"      [{src_names[f['src']]}]")
            it.setData(Qt.UserRole, i)
            self.list.addItem(it)
        if found:
            self.list.setCurrentRow(0)
            self.ok.setEnabled(True)
            if not self.name.text().strip():
                self.name.setText(found[0].get("title") or self.page_title or "video")
            self.state.setText(f"{len(found)} option{'s' if len(found) != 1 else ''} found. "
                               "The first one is the best quality.")
        else:
            self.state.setText("No downloadable video found. " + " ".join(notes[:2]))
        if found and notes:
            self.state.setText(self.state.text() + "  (" + notes[0][:120] + ")")

    def submit(self, start):
        it = self.list.currentItem()
        if not it:
            return
        f = self.options[it.data(Qt.UserRole)]
        title = self.name.text().strip() or f.get("title") or self.page_title or "video"
        try:
            if f["src"] == "site":
                info = dict(f["info"], title=title)
                self.m.add_media(info, f["option"], start=start, referrer=self.referrer,
                                 cookies=self.cookies, source=self.source)
            elif f["src"] == "stream":
                self.m.add_stream(f["url"], title=title, height=f.get("height"), start=start,
                                  referrer=self.referrer, cookies=self.cookies,
                                  source=self.source, page_url=self.url.text().strip() or None)
            else:
                self.m.add(f["url"], filename=media.title_filename(title, f["container"]),
                           referrer=self.referrer, cookies=self.cookies, start=start,
                           source=self.source)
        except Exception as e:  # noqa: BLE001
            self.state.setText(f"Couldn't add: {e}")
            return
        self.accept()


# --------------------------------------------------------------------------- grabber
class GrabberDialog(Dialog):
    TYPES = ["Compressed", "Video", "Music", "Documents", "Programs", "Images", "Other"]

    def __init__(self, parent, manager, page_url="", links=None, referrer=None):
        super().__init__(parent, "Grab files from a page",
                         "Scan a web page (and pages it links to) for files, then pick what to "
                         "download.", "layers", C["accent2"])
        self.m, self.referrer = manager, referrer
        self.items = []
        self._stop = threading.Event()
        self.resize(820, 680)
        row = QHBoxLayout()
        self.url = QLineEdit(page_url)
        self.url.setPlaceholderText("https://example.com/downloads/")
        row.addWidget(self.url, 1)
        row.addWidget(QLabel("Depth"))
        self.depth = QSpinBox()
        self.depth.setRange(0, 3)
        self.depth.setToolTip("0 = this page only · 1–3 = also follow links on the same site")
        row.addWidget(self.depth)
        self.scan_btn = button("Scan", None, "search")
        self.scan_btn.clicked.connect(self.scan)
        row.addWidget(self.scan_btn)
        self.root.addLayout(row)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.type_btns = {}
        for t in self.TYPES:
            b = QPushButton(t)
            b.setCheckable(True)
            b.setChecked(t not in ("Images", "Other"))
            b.setCursor(Qt.PointingHandCursor)
            col = CAT_COLORS.get(t, C["accent2"] if t == "Images" else C["muted"])
            b.setStyleSheet(f"QPushButton{{padding:5px 11px;border-radius:14px;}}"
                            f"QPushButton:checked{{{chip_style(col)} border:1px solid {col};}}")
            b.toggled.connect(self.refilter)
            chips.addWidget(b)
            self.type_btns[t] = b
        chips.addStretch()
        self.root.addLayout(chips)
        frow = QHBoxLayout()
        self.ext = QLineEdit()
        self.ext.setPlaceholderText("Extra extensions, e.g. srt, torrent")
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("Name contains / regex, e.g. 2026|final")
        for w in (self.ext, self.pattern):
            w.textChanged.connect(self.refilter)
            frow.addWidget(w)
        self.root.addLayout(frow)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Type", "Address"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 100)
        self.tree.setStyleSheet(f"QTreeWidget{{background:{C['panel']};border:1px solid "
                                f"{C['border2']};border-radius:10px;}} QHeaderView::section{{"
                                f"background:{C['card']};color:{C['muted']};border:none;"
                                f"padding:6px;}}")
        self.tree.itemChanged.connect(lambda *_: self.count())
        self.root.addWidget(self.tree, 1)
        brow = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        brow.addWidget(self.status, 1)
        alln = button("All", "Ghost")
        alln.clicked.connect(lambda: self.set_all(True))
        none = button("None", "Ghost")
        none.clicked.connect(lambda: self.set_all(False))
        brow.addWidget(alln)
        brow.addWidget(none)
        self.root.addLayout(brow)
        self.auto = Toggle("Sort into category folders")
        self.auto.setChecked(True)
        self.start = Toggle("Start right away")
        self.start.setChecked(True)
        orow = QHBoxLayout()
        orow.addWidget(self.auto)
        orow.addWidget(self.start)
        orow.addStretch()
        self.root.addLayout(orow)
        cancel = button("Cancel", "Ghost")
        cancel.clicked.connect(self.reject)
        self.ok = button("Add downloads", "Primary", "plus")
        self.ok.clicked.connect(self.submit)
        self.add_buttons(cancel, self.ok)
        self.sig = _Sig()
        self.sig.done.connect(self._scanned)
        self.sig.progress.connect(lambda _t, p: self.status.setText(
            f"Scanning… {p[0]} page{'s' if p[0] != 1 else ''}, {p[1]} files"))
        self._token = 0
        if links:
            self._load([{"url": u, "text": "", "type": grabber.classify(u),
                         "page": referrer or ""} for u in links])
        elif page_url:
            QTimer.singleShot(50, self.scan)
        self.count()

    def scan(self):
        url = self.url.text().strip()
        if not is_http_url(url):
            self.status.setText("Enter a page address (http/https).")
            return
        self._token += 1
        token = self._token
        self.scan_btn.setEnabled(False)
        self.status.setText("Scanning…")
        headers = {"User-Agent": self.m.settings["user_agent"]}
        depth, opener = self.depth.value(), self.m.opener

        def work():
            try:
                res = grabber.grab(url, opener, headers, depth=depth, stop=self._stop,
                                   progress=lambda p, n, _u: self.sig.progress.emit(token, (p, n)))
            except Exception as e:  # noqa: BLE001
                res = e
            self.sig.done.emit(token, res)
        threading.Thread(target=work, daemon=True).start()

    def _scanned(self, token, res):
        if token != self._token:
            return
        self.scan_btn.setEnabled(True)
        if isinstance(res, Exception):
            self.status.setText(f"Couldn't read the page: {res}")
            return
        self.referrer = self.url.text().strip()
        self._load(res)

    def _load(self, items):
        self.items = items
        self.refilter()

    def refilter(self):
        types = {t for t, b in self.type_btns.items() if b.isChecked()}
        self.tree.blockSignals(True)
        self.tree.clear()
        hist = self.m.history
        for it in self.items:
            if not grabber.matches(it, types, self.ext.text(), self.pattern.text().strip()):
                continue
            name = urllib.parse.unquote(urllib.parse.urlsplit(it["url"]).path.rsplit("/", 1)[-1])
            row = QTreeWidgetItem([name or it["url"], it["type"], it["url"]])
            dup = self.m.find_url(it["url"]) or (hist.find_url(it["url"]) if hist else None)
            row.setCheckState(0, Qt.Unchecked if dup else Qt.Checked)
            if dup:
                row.setToolTip(0, "Already downloaded or in your list")
                row.setForeground(0, QColor(C["faint"]))
            row.setData(0, Qt.UserRole, it["url"])
            self.tree.addTopLevelItem(row)
        self.tree.blockSignals(False)
        self.count()

    def set_all(self, on):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Checked if on else Qt.Unchecked)
        self.tree.blockSignals(False)
        self.count()

    def selected(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == Qt.Checked:
                out.append(it.data(0, Qt.UserRole))
        return out

    def count(self):
        n = len(self.selected())
        shown = self.tree.topLevelItemCount()
        if self.items:
            self.status.setText(f"{len(self.items)} files found · {shown} shown · {n} selected")
        self.ok.setEnabled(n > 0)
        self.ok.setText(f"Add {n} download{'s' if n != 1 else ''}" if n else "Add downloads")

    def submit(self):
        for u in self.selected():
            if not self.m.find_url(u):
                self.m.add(u, save_dir=None if self.auto.isChecked()
                           else self.m.settings["download_dir"], referrer=self.referrer,
                           start=self.start.isChecked(), source="grabber")
        self._stop.set()
        self.accept()

    def reject(self):
        self._stop.set()
        super().reject()


# --------------------------------------------------------------------------- statistics
class BarChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.data = []

    def set_data(self, data):
        self.data = data
        self.update()

    def paintEvent(self, _e):
        if not self.data:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(self.font())
        f.setPixelSize(10)
        p.setFont(f)
        w, h = self.width(), self.height()
        top = max(v for _l, v in self.data) or 1
        n = len(self.data)
        gap = 6
        bw = (w - gap * (n - 1)) / n
        for i, (label, v) in enumerate(self.data):
            x = i * (bw + gap)
            bh = (h - 34) * v / top
            r = QRectF(x, h - 20 - bh, bw, max(bh, 2))
            path = QPainterPath()
            path.addRoundedRect(r, 4, 4)
            p.fillPath(path, accent_gradient(r) if v else QColor(C["border"]))
            p.setPen(QColor(C["faint"]))
            p.drawText(QRectF(x - 4, h - 16, bw + 8, 14), Qt.AlignCenter, label.split()[-1])
            if v and v == top:
                p.setPen(QColor(C["muted"]))
                p.drawText(QRectF(x - 20, r.top() - 14, bw + 40, 12), Qt.AlignCenter, human_size(v))
        p.end()


class StatsView(QWidget):
    def __init__(self, manager):
        super().__init__()
        self.m = manager
        self.setObjectName("Clear")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.cards = {}
        for key, title in (("today", "TODAY"), ("week", "LAST 7 DAYS"), ("all", "ALL TIME"),
                           ("best", "FASTEST DOWNLOAD")):
            fr = QFrame()
            fr.setObjectName("Panel")
            fl = QVBoxLayout(fr)
            fl.setContentsMargins(16, 12, 16, 12)
            cap = QLabel(title)
            cap.setObjectName("FieldLabel")
            big = QLabel("—")
            big.setStyleSheet("font-size: 21px; font-weight: 700;")
            sub = QLabel("")
            sub.setObjectName("Faint")
            fl.addWidget(cap)
            fl.addWidget(big)
            fl.addWidget(sub)
            cards.addWidget(fr)
            self.cards[key] = (big, sub)
        lay.addLayout(cards)
        chart = QFrame()
        chart.setObjectName("Panel")
        cl = QVBoxLayout(chart)
        cl.setContentsMargins(16, 12, 16, 12)
        cap = QLabel("DOWNLOADED PER DAY")
        cap.setObjectName("FieldLabel")
        cl.addWidget(cap)
        self.chart = BarChart()
        cl.addWidget(self.chart)
        lay.addWidget(chart)
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        hosts = QFrame()
        hosts.setObjectName("Panel")
        hl = QVBoxLayout(hosts)
        hl.setContentsMargins(16, 12, 16, 12)
        cap = QLabel("TOP SITES")
        cap.setObjectName("FieldLabel")
        hl.addWidget(cap)
        self.hosts = QGridLayout()
        self.hosts.setVerticalSpacing(6)
        hl.addLayout(self.hosts)
        hl.addStretch()
        bottom.addWidget(hosts, 1)
        hist = QFrame()
        hist.setObjectName("Panel")
        hv = QVBoxLayout(hist)
        hv.setContentsMargins(16, 12, 16, 12)
        hrow = QHBoxLayout()
        cap = QLabel("HISTORY")
        cap.setObjectName("FieldLabel")
        hrow.addWidget(cap)
        hrow.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search history")
        self.search.setFixedWidth(200)
        self.search.textChanged.connect(self.refresh_history)
        hrow.addWidget(self.search)
        clear = button("Clear", "Ghost")
        clear.clicked.connect(self._clear)
        hrow.addWidget(clear)
        hv.addLayout(hrow)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Size", "Speed", "When"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setShowGrid(False)
        self.table.setStyleSheet(f"QTableWidget{{background:transparent;border:none;}}"
                                 f"QHeaderView::section{{background:transparent;color:"
                                 f"{C['faint']};border:none;padding:4px;}}")
        self.table.doubleClicked.connect(self._open)
        hv.addWidget(self.table)
        bottom.addWidget(hist, 2)
        lay.addLayout(bottom, 1)
        self._rows = []

    def refresh(self):
        h = self.m.history
        if not h:
            return
        st = h.stats()
        for key in ("today", "week", "all"):
            big, sub = self.cards[key]
            big.setText(human_size(st[key]["bytes"]))
            sub.setText(f"{st[key]['count']} files · avg {human_size(st[key]['avg_speed'])}/s")
        big, sub = self.cards["best"]
        big.setText(f"{human_size(st['best_speed'])}/s")
        sub.setText(f"{st['threats']} threat{'s' if st['threats'] != 1 else ''} caught")
        self.chart.set_data(st["daily"])
        while self.hosts.count():
            w = self.hosts.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, (host, count, size, speed) in enumerate(st["hosts"][:6]):
            a = QLabel(host or "—")
            b = QLabel(f"{count} · {human_size(size)}")
            b.setObjectName("Muted")
            b.setAlignment(Qt.AlignRight)
            self.hosts.addWidget(a, i, 0)
            self.hosts.addWidget(b, i, 1)
        self.refresh_history()

    def refresh_history(self):
        h = self.m.history
        if not h:
            return
        self._rows = h.search(self.search.text().strip())
        self.table.setRowCount(len(self._rows))
        for r, (name, url, path, size, fin, speed) in enumerate(self._rows):
            vals = [name, human_size(size), f"{human_size(speed)}/s",
                    time.strftime("%b %d %H:%M", time.localtime(fin or 0))]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setToolTip(f"{path}\n{url}")
                self.table.setItem(r, c, item)

    def _open(self, idx):
        if 0 <= idx.row() < len(self._rows):
            reveal_in_folder(self._rows[idx.row()][2])

    def _clear(self):
        if self.m.history:
            self.m.history.clear()
            self.refresh()
