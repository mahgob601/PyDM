"""Main application window."""
import collections
import os
import time

from PySide6.QtCore import (QAbstractListModel, QItemSelectionModel, QModelIndex, QSettings,
                            QSortFilterProxyModel, Qt, QTimer)
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLayout, QLineEdit, QListView, QMainWindow, QMenu,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

import threading

from ..core import notify, remote, security, updater
from ..core.power import SleepInhibitor
from ..core.util import (APP_NAME, CATEGORY_ORDER, DOWNLOAD_EXTS, VERSION, ext_of, human_size,
                         human_time, is_http_url, open_path, reveal_in_folder)
from .dialogs import (BatchDialog, ConfirmDialog, NewDownloadDialog, PairingDialog,
                      SettingsDialog, button)
from .extra import GrabberDialog, StatsView, VideoDialog
from .theme import C, CAT_ICONS, LEVEL_COLORS, app_icon, icon, pixmap
from .widgets import (ConnBars, DownloadDelegate, SegmentMap, Sparkline, ToastHost, chip_style,
                      status_text, tile_pixmap, time_ago)

VIEWS = [("all", "All downloads", "inbox"), ("active", "Active", "activity"),
         ("completed", "Completed", "check-circle"), ("failed", "Needs attention", "alert"),
         ("stats", "Statistics", "gauge")]


# --------------------------------------------------------------------------- models
class DownloadModel(QAbstractListModel):
    def __init__(self, manager):
        super().__init__()
        self.m = manager
        self.items = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        d = self.items[index.row()]
        if role == Qt.UserRole:
            return d
        if role == Qt.DisplayRole:
            return d.filename
        return None

    def refresh(self):
        new = self.m.visible()
        if [d.id for d in new] != [d.id for d in self.items]:
            self.beginResetModel()
            self.items = new
            self.endResetModel()
            return True
        if self.items:
            self.dataChanged.emit(self.index(0), self.index(len(self.items) - 1))
        return False


def matches_view(d, view):
    if view in ("all", "stats"):
        return True
    if view == "active":
        return d.status in ("Queued", "Connecting", "Downloading", "Verifying")
    if view == "completed":
        return d.status == "Completed"
    if view == "failed":
        return d.status == "Error" or (d.security or {}).get("level") in ("caution", "danger")
    if view.startswith("cat:"):
        return d.category == view[4:]
    return True


class FilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.view = "all"
        self.text = ""

    def filterAcceptsRow(self, row, parent):
        d = self.sourceModel().items[row]
        if not matches_view(d, self.view):
            return False
        t = self.text
        return not t or t in d.filename.lower() or t in d.url.lower()


# --------------------------------------------------------------------------- sidebar
class NavButton(QPushButton):
    def __init__(self, text, icon_name, key):
        super().__init__()
        self.key, self.icon_name = key, icon_name
        self.setObjectName("Nav")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(38)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(11)
        self.ic = QLabel()
        self.label = QLabel(text)
        self.count = QLabel("")
        for w in (self.ic, self.label, self.count):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self.ic)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.count)
        self.toggled.connect(self._style)
        self._style(False)

    def _style(self, on):
        color = C["text"] if on else C["muted"]
        self.ic.setPixmap(pixmap(self.icon_name, C["accent2"] if on else C["muted"], 17))
        self.label.setStyleSheet(f"color: {color}; font-weight: {600 if on else 500};")
        self.count.setStyleSheet(f"color: {C['faint']}; font-size: 11.5px;")

    def set_count(self, n):
        self.count.setText(str(n) if n else "")


class DetailsPanel(QFrame):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.d = None
        self.setObjectName("Details")
        self.setFixedWidth(360)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        empty = QWidget()
        empty.setObjectName("Clear")
        el = QVBoxLayout(empty)
        el.addStretch()
        ic = QLabel()
        ic.setPixmap(pixmap("file", C["faint"], 40))
        ic.setAlignment(Qt.AlignCenter)
        el.addWidget(ic)
        t = QLabel("Select a download")
        t.setAlignment(Qt.AlignCenter)
        t.setObjectName("H2")
        el.addWidget(t)
        s = QLabel("Details, live speed and the\nsecurity report appear here.")
        s.setAlignment(Qt.AlignCenter)
        s.setObjectName("Muted")
        el.addWidget(s)
        el.addStretch()
        self.stack.addWidget(empty)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("Clear")
        lay = QVBoxLayout(body)
        lay.setSizeConstraint(QLayout.SetMinimumSize)
        lay.setContentsMargins(20, 22, 20, 20)
        lay.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.tile = QLabel()
        self.tile.setFixedSize(48, 48)
        head.addWidget(self.tile, 0, Qt.AlignTop)
        hc = QVBoxLayout()
        hc.setSpacing(6)
        self.name = QLabel()
        self.name.setObjectName("H2")
        self.name.setWordWrap(True)
        self.name.setFixedWidth(250)
        hc.addWidget(self.name)
        self.chip = QLabel()
        hc.addWidget(self.chip, 0, Qt.AlignLeft)
        head.addLayout(hc, 1)
        lay.addLayout(head)

        prow = QHBoxLayout()
        self.pct = QLabel()
        self.pct.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.pct_sub = QLabel()
        self.pct_sub.setObjectName("Muted")
        prow.addWidget(self.pct)
        prow.addWidget(self.pct_sub, 1, Qt.AlignBottom | Qt.AlignRight)
        lay.addLayout(prow)
        self.segmap = SegmentMap()
        lay.addWidget(self.segmap)
        self.seg_caption = QLabel()
        self.seg_caption.setObjectName("Faint")
        lay.addWidget(self.seg_caption)

        speed = QFrame()
        speed.setObjectName("Panel")
        sl = QVBoxLayout(speed)
        sl.setContentsMargins(14, 12, 14, 10)
        srow = QHBoxLayout()
        cap = QLabel("SPEED")
        cap.setObjectName("FieldLabel")
        srow.addWidget(cap)
        srow.addStretch()
        self.speed_now = QLabel()
        self.speed_now.setStyleSheet("font-weight: 650;")
        srow.addWidget(self.speed_now)
        sl.addLayout(srow)
        self.spark = Sparkline(height=70)
        sl.addWidget(self.spark)
        self.speed_peak = QLabel()
        self.speed_peak.setObjectName("Faint")
        sl.addWidget(self.speed_peak)
        lay.addWidget(speed)

        conn = QFrame()
        conn.setObjectName("Panel")
        cl = QVBoxLayout(conn)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)
        crow = QHBoxLayout()
        cap2 = QLabel("CONNECTIONS")
        cap2.setObjectName("FieldLabel")
        crow.addWidget(cap2)
        crow.addStretch()
        self.conn_mode = QLabel()
        crow.addWidget(self.conn_mode)
        cl.addLayout(crow)
        self.conn_bars = ConnBars()
        cl.addWidget(self.conn_bars)
        self.conn_caption = QLabel()
        self.conn_caption.setObjectName("Faint")
        self.conn_caption.setWordWrap(True)
        self.conn_caption.setFixedWidth(290)
        cl.addWidget(self.conn_caption)
        self.conn_panel = conn
        lay.addWidget(conn)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(9)
        self.info = {}
        for i, key in enumerate(["Size", "Time left", "Mode", "Resume", "Speed limit",
                                 "Source", "Added", "Saved to", "Extracted to", "Address"]):
            k = QLabel(key)
            k.setObjectName("Muted")
            v = QLabel()
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self.info[key] = v
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        sec = QFrame()
        sec.setObjectName("Panel")
        self.sec_lay = QVBoxLayout(sec)
        self.sec_lay.setContentsMargins(14, 12, 14, 12)
        self.sec_lay.setSpacing(8)
        sh = QHBoxLayout()
        self.sec_icon = QLabel()
        self.sec_title = QLabel()
        self.sec_title.setStyleSheet("font-weight: 650;")
        sh.addWidget(self.sec_icon)
        sh.addWidget(self.sec_title, 1)
        self.sec_lay.addLayout(sh)
        self.sec_items = QVBoxLayout()
        self.sec_items.setSpacing(6)
        self.sec_lay.addLayout(self.sec_items)
        hrow = QHBoxLayout()
        self.sha = QLabel()
        self.sha.setStyleSheet(f"font-family: Consolas, 'JetBrains Mono', monospace; "
                               f"font-size: 11px; color: {C['muted']};")
        self.sha.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hrow.addWidget(self.sha, 1)
        self.copy_sha = QPushButton()
        self.copy_sha.setObjectName("Icon")
        self.copy_sha.setIcon(icon("copy", C["muted"], 14))
        self.copy_sha.setToolTip("Copy SHA-256")
        self.copy_sha.clicked.connect(lambda: self.win.copy_text(self.d.sha256, "SHA-256 copied"))
        hrow.addWidget(self.copy_sha)
        self.sec_lay.addLayout(hrow)
        lay.addWidget(sec)
        self._sec_key = None

        acts = QGridLayout()
        acts.setSpacing(8)
        self.primary = button("", "Primary")
        self.primary.clicked.connect(self._primary)
        self.folder = button("Show in folder", None, "folder")
        self.folder.clicked.connect(lambda: self.win.reveal(self.d))
        self.refresh = button("New address", None, "refresh")
        self.refresh.setToolTip("Link expired? Paste a fresh one and keep your progress.")
        self.refresh.clicked.connect(lambda: self.win.refresh_address(self.d))
        self.remove = button("Remove", "Danger", "trash", "#fda4af")
        self.remove.clicked.connect(lambda: self.win.remove([self.d]))
        acts.addWidget(self.primary, 0, 0, 1, 2)
        acts.addWidget(self.folder, 1, 0)
        acts.addWidget(self.refresh, 1, 1)
        acts.addWidget(self.remove, 2, 0, 1, 2)
        lay.addLayout(acts)
        lay.addStretch()
        scroll.setWidget(body)
        scroll.viewport().setObjectName("Clear")
        self.stack.addWidget(scroll)

    def _primary(self):
        d = self.d
        if not d:
            return
        if d.status == "Completed":
            self.win.open_file(d)
        elif d.is_running() or d.status == "Queued":
            self.win.m.pause(d)
        else:
            self.win.m.resume(d)
        self.update_view()

    def show_download(self, d):
        self.d = d
        self._sec_key = None
        self.stack.setCurrentIndex(1 if d else 0)
        self.update_view()

    def update_view(self):
        d = self.d
        if not d:
            return
        self.tile.setPixmap(tile_pixmap(d.category, 48))
        self.name.setText(d.filename)
        text, color = status_text(d)
        label = {"Done": "Completed", "Threat": "Threat detected", "Review": "Review warnings"}
        self.chip.setText(label.get(text, d.status if d.status != "Downloading" else "Downloading"))
        self.chip.setStyleSheet(chip_style(color))
        done, total = d.downloaded, d.expected_total
        self.pct.setText(f"{d.progress * 100:.0f}%" if total or d.status == "Completed"
                         or d.kind in ("hls", "media") else "—")
        self.pct_sub.setText(f"{human_size(done if d.status != 'Completed' else total)} of "
                             f"{human_size(total)}")
        self.segmap.set_download(d)
        with d.lock:
            parts = len(d.segments)
        ptxt = f"{parts} part{'s' if parts != 1 else ''}"
        if d.kind == "hls":
            ptxt = f"{d.hls_done}/{d.hls_total} segments"
        elif d.kind == "media":
            ptxt = " + ".join(f"{os.path.splitext(k.filename)[0]} {k.progress * 100:.0f}%"
                              for k in d.kids()) or "merged"
        elif d.kind == "ytdlp":
            ptxt = "via yt-dlp"
        self.seg_caption.setText(f"{d.active_connections} active connections · {ptxt}"
                                 if d.is_running() else ptxt)
        self.speed_now.setText(f"{human_size(d.speed)}/s" if d.is_running() else "Idle")
        self.spark.set_values(d.history)
        self.speed_peak.setText(f"Peak {human_size(max(d.history))}/s · last 60 seconds"
                                if max(d.history) > 0 else "No activity in the last minute")
        i = self.info
        i["Size"].setText(("~" if d.kind == "hls" and d.status != "Completed" else "") +
                          human_size(total) if total is not None else "Unknown")
        i["Time left"].setText(human_time(d.eta) if d.is_running() and d.eta else "—")
        mode = "Streaming order" if d.sequential else "Fastest"
        if d.extract:
            mode += " · extract"
        i["Mode"].setText(mode)
        i["Extracted to"].setText(self._elide(d.extracted_to, 26) if d.extracted_to else "—")
        i["Extracted to"].setToolTip(d.extracted_to or "")
        self._update_connections(d)
        i["Resume"].setText("Supported" if d.resumable or d.kind in ("hls", "media", "ytdlp") else
                            ("Not supported" if d.total is not None or d.path else "—"))
        i["Speed limit"].setText(f"{d.speed_limit_kbps} KB/s" if d.speed_limit_kbps else
                                 "Global")
        i["Source"].setText(d.source.capitalize())
        i["Added"].setText(time_ago(d.added))
        where = os.path.dirname(d.path) if d.path else (d.save_dir or "Auto (category folder)")
        i["Saved to"].setText(self._elide(where, 26))
        i["Saved to"].setToolTip(where)
        i["Address"].setText(self._elide(d.url, 26))
        i["Address"].setToolTip(d.url)

        if d.status == "Completed":
            self.primary.setText("Open file")
            self.primary.setIcon(icon("external", "white", 16))
        elif d.is_running() or d.status == "Queued":
            self.primary.setText("Pause")
            self.primary.setIcon(icon("pause", "white", 16))
        else:
            self.primary.setText("Resume" if d.status == "Paused" else
                                 ("Retry now" if d.retry_at else "Retry"))
            self.primary.setIcon(icon("play", "white", 16))
        self.primary.setEnabled(d.status != "Verifying")
        self.refresh.setEnabled(d.status in ("Paused", "Error") and not d.is_running())
        self._update_security(d)

    def _update_connections(self, d):
        running = d.is_running() and d.status == "Downloading"
        rows = d.worker_stats() if running else []
        self.conn_bars.set_rows(rows)
        if d.auto_tune:
            state = {"tuning": "finding the best count", "optimal": "optimal",
                     "max": "at maximum", "server limit": "server limit reached"}.get(
                d.tuning, "")
            self.conn_mode.setText(f"Auto · {len(rows)}/{d.connections}" if running else
                                   f"Auto · up to {d.connections}")
            self.conn_mode.setToolTip(state)
        else:
            self.conn_mode.setText(f"Fixed · {d.connections}")
        self.conn_mode.setStyleSheet(chip_style(C["accent2"] if d.auto_tune else C["muted"]))
        bits = []
        if running and d.auto_tune and d.tuning:
            bits.append({"tuning": "Auto-tuning: adding connections while speed keeps rising.",
                         "optimal": "Auto-tuned: more connections wouldn't be faster.",
                         "max": "Using the maximum number of connections.",
                         "server limit": "The server limits connections — PyDM remembers "
                                         "this for next time."}.get(d.tuning, ""))
        if len(d.sources) > 1:
            bits.append(f"Pulling from {len(d.sources)} sources (orange = mirror).")
        bad = [f"{u.split('/')[2]}: {s}" for u, s in d.mirror_status.items() if s != "ok"]
        if bad:
            bits.append("Mirrors skipped — " + "; ".join(bad))
        if d.conn_restarts:
            bits.append(f"Replaced {d.conn_restarts} slow connection"
                        f"{'s' if d.conn_restarts != 1 else ''}.")
        if not running:
            bits.append("Shown while downloading." if d.status != "Completed" else
                        "Download finished.")
        self.conn_caption.setText(" ".join(b for b in bits if b))

    def _update_security(self, d):
        sec = d.security
        key = (d.id, d.status, id(sec))
        if key == self._sec_key:
            return
        self._sec_key = key
        while self.sec_items.count():
            w = self.sec_items.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not sec:
            pre = security.name_findings(d.raw_name)
            if d.status == "Verifying":
                title, lvl = "Checking file…", "info"
            else:
                title, lvl = "Checked when the download finishes", "info"
            self.sec_icon.setPixmap(pixmap("shield", C["muted"], 18))
            self.sec_title.setText(title)
            self.sec_title.setStyleSheet("font-weight: 650;")
            for f in pre:
                self._finding_row(f)
            self.sha.setText("")
            self.copy_sha.setVisible(False)
            return
        lvl = sec["level"]
        titles = {"safe": "No problems found", "caution": "Review before opening",
                  "danger": "Potential threat"}
        self.sec_icon.setPixmap(pixmap("shield-check" if lvl == "safe" else "shield-alert",
                                       LEVEL_COLORS[lvl], 18))
        self.sec_title.setText(titles.get(lvl, "Security report"))
        self.sec_title.setStyleSheet(f"font-weight: 650; color: {LEVEL_COLORS[lvl]};")
        for f in sec["findings"]:
            self._finding_row(f)
        extras = []
        if sec.get("motw"):
            extras.append("Marked as downloaded from the Internet")
        av = sec.get("av")
        if av and av["status"] in ("unavailable", "error"):
            extras.append(av["detail"])
        for e in extras:
            t = QLabel(e)
            t.setObjectName("Faint")
            t.setWordWrap(True)
            t.setFixedWidth(282)
            self.sec_items.addWidget(t)
        self.sha.setText(f"SHA-256  {d.sha256[:20]}…" if d.sha256 else "")
        self.copy_sha.setVisible(bool(d.sha256))

    def _finding_row(self, f):
        row = QWidget()
        row.setObjectName("Clear")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {LEVEL_COLORS[f['level']]}; font-size: 9px;")
        dot.setAlignment(Qt.AlignTop)
        dot.setFixedWidth(10)
        rl.addWidget(dot)
        t = QLabel(f["text"])
        t.setWordWrap(True)
        t.setFixedWidth(264)
        t.setStyleSheet("color: #cfd5e2; font-size: 12px;")
        rl.addWidget(t)
        rl.addStretch()
        self.sec_items.addWidget(row)

    @staticmethod
    def _elide(s, n):
        return s if len(s) <= n else s[:n // 2 - 1] + "…" + s[-(n // 2):]


# --------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, manager, bridge=None, bridge_error=None):
        super().__init__()
        self.m, self.bridge, self.bridge_error = manager, bridge, bridge_error
        self.view = "all"
        self._quitting = False
        self._own_clip = ""
        self._tray_hint_shown = False
        self.global_history = collections.deque([0.0] * 90, maxlen=90)
        self.sleep = SleepInhibitor()
        self.remote_srv = None
        self._update_found = None
        self.setWindowTitle(f"{APP_NAME}")
        self.setWindowIcon(app_icon())
        self.resize(1320, 800)
        self.setMinimumSize(960, 600)
        self.setAcceptDrops(True)

        self.model = DownloadModel(manager)
        self.proxy = FilterProxy()
        self.proxy.setSourceModel(self.model)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addLayout(body, 1)
        body.addWidget(self._build_sidebar())
        self.content = self._build_content()
        body.addWidget(self.content, 1)
        self.details = DetailsPanel(self)
        body.addWidget(self.details)
        outer.addWidget(self._build_footer())
        self.toasts = ToastHost(self.content)

        self._build_tray()
        self._shortcuts()
        cb = QApplication.clipboard()
        self._last_clip = cb.text().strip()
        cb.dataChanged.connect(self._clipboard_changed)

        st = QSettings(APP_NAME, APP_NAME)
        geo = st.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        self.timer = QTimer(self, interval=500)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.apply_remote()
        if self.m.settings["check_updates"]:
            QTimer.singleShot(4000, self.check_updates)
        self.tick()

    # ------------------------------------------------------------------ layout
    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(236)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(12, 18, 12, 14)
        lay.setSpacing(2)
        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 0, 14)
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(30, 30))
        brand.addWidget(logo)
        name = QLabel(APP_NAME)
        name.setObjectName("Brand")
        brand.addWidget(name)
        pro = QLabel("PRO")
        pro.setObjectName("ProChip")
        brand.addWidget(pro)
        brand.addStretch()
        lay.addLayout(brand)

        # Navigation scrolls when the window is short (large fonts / 125-150% scaling on
        # Windows), so the speed card below always keeps its full height.
        nav_box = QWidget()
        nav_box.setObjectName("SideNav")
        nl = QVBoxLayout(nav_box)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.setSpacing(2)
        self.nav = {}
        for key, text, ic in VIEWS:
            b = NavButton(text, ic, key)
            b.clicked.connect(lambda _=False, k=key: self.set_view(k))
            nl.addWidget(b)
            self.nav[key] = b
        sec = QLabel("CATEGORIES")
        sec.setObjectName("Section")
        nl.addWidget(sec)
        for cat in CATEGORY_ORDER:
            b = NavButton(cat, CAT_ICONS[cat], f"cat:{cat}")
            b.clicked.connect(lambda _=False, k=f"cat:{cat}": self.set_view(k))
            nl.addWidget(b)
            self.nav[f"cat:{cat}"] = b
        nl.addStretch()
        nav_scroll = QScrollArea()
        nav_scroll.setObjectName("SideScroll")
        nav_scroll.setWidget(nav_box)
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                 "{ background: transparent; }")
        lay.addWidget(nav_scroll, 1)

        card = QFrame()
        card.setObjectName("SideCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(4)
        cap = QLabel("TOTAL SPEED")
        cap.setObjectName("FieldLabel")
        cl.addWidget(cap)
        self.side_speed = QLabel("0 B/s")
        self.side_speed.setObjectName("BigNumber")
        cl.addWidget(self.side_speed)
        self.side_spark = Sparkline(height=38)
        cl.addWidget(self.side_spark)
        self.limit_btn = QPushButton()
        self.limit_btn.setObjectName("Ghost")
        self.limit_btn.setCursor(Qt.PointingHandCursor)
        self.limit_btn.setIcon(icon("gauge", C["muted"], 15))
        self.limit_btn.clicked.connect(self._limit_menu)
        cl.addWidget(self.limit_btn, 0, Qt.AlignLeft)
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay.addSpacing(8)
        lay.addWidget(card)
        lay.addSpacing(8)
        settings = NavButton("Settings", "settings", "settings")
        settings.setCheckable(False)
        settings.clicked.connect(self.open_settings)
        lay.addWidget(settings)
        return side

    def _build_content(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 20, 12)
        lay.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(8)
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName("Title")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("Muted")
        tcol.addWidget(self.title)
        tcol.addWidget(self.subtitle)
        head.addLayout(tcol)
        head.addStretch()
        video = button("Video", None, "film")
        video.setToolTip("Download a video from a website or stream (Ctrl+Shift+V)")
        video.clicked.connect(lambda: self.open_video())
        head.addWidget(video)
        grab = button("Grab", None, "search")
        grab.setToolTip("Grab files from a web page (Ctrl+G)")
        grab.clicked.connect(lambda: self.open_grabber())
        head.addWidget(grab)
        batch = button("Batch", None, "layers")
        batch.setToolTip("Add many links at once (Ctrl+B)")
        batch.clicked.connect(lambda: self.open_batch())
        head.addWidget(batch)
        new = button("New download", "Primary", "plus")
        new.setToolTip("Ctrl+N")
        new.clicked.connect(lambda: self.open_new())
        head.addWidget(new)
        lay.addLayout(head)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Search by name or address   Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(200)
        self.search.setMaximumWidth(380)
        self.search.textChanged.connect(self._search)
        sicon = QLabel(self.search)
        sicon.setPixmap(pixmap("search", C["faint"], 16))
        sicon.move(12, 10)
        sicon.setAttribute(Qt.WA_TransparentForMouseEvents)
        bar.addWidget(self.search, 1)
        bar.addStretch()
        for ic, text, cb in (("play", "Resume all", self.m.resume_all),
                             ("pause", "Pause all", self.m.pause_all),
                             ("broom", "Clear completed", self.clear_completed)):
            b = button(text, "Ghost", ic, C["muted"])
            b.clicked.connect(cb)
            bar.addWidget(b)
        lay.addLayout(bar)

        self.list_stack = QStackedWidget()
        self.list = QListView()
        self.list.setObjectName("Downloads")
        self.list.setModel(self.proxy)
        self.list.setItemDelegate(DownloadDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.doubleClicked.connect(lambda idx: self._activate(idx.data(Qt.UserRole)))
        self.list.selectionModel().selectionChanged.connect(self._selection_changed)
        self.list_stack.addWidget(self.list)

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch()
        ic = QLabel()
        ic.setPixmap(app_icon().pixmap(72, 72))
        ic.setAlignment(Qt.AlignCenter)
        el.addWidget(ic)
        el.addSpacing(10)
        self.empty_title = QLabel("Nothing here yet")
        self.empty_title.setObjectName("Title")
        self.empty_title.setAlignment(Qt.AlignCenter)
        el.addWidget(self.empty_title)
        self.empty_sub = QLabel("Paste a link with Ctrl+V, drop it onto this window,\nor "
                                "right-click a link in your browser.")
        self.empty_sub.setObjectName("Muted")
        self.empty_sub.setAlignment(Qt.AlignCenter)
        el.addWidget(self.empty_sub)
        el.addSpacing(12)
        eb = button("New download", "Primary", "plus")
        eb.clicked.connect(lambda: self.open_new())
        el.addWidget(eb, 0, Qt.AlignCenter)
        el.addStretch()
        self.list_stack.addWidget(empty)
        self.stats = StatsView(self.m)
        self.list_stack.addWidget(self.stats)
        lay.addWidget(self.list_stack, 1)
        return w

    def _build_footer(self):
        f = QFrame()
        f.setObjectName("Footer")
        f.setFixedHeight(34)
        lay = QHBoxLayout(f)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(18)
        self.f_dot = QLabel("●")
        self.f_browser = QLabel()
        lay.addWidget(self.f_dot)
        lay.addSpacing(-12)
        lay.addWidget(self.f_browser)
        self.f_stats = QLabel()
        lay.addWidget(self.f_stats)
        lay.addStretch()
        self.f_bw = QLabel()
        lay.addWidget(self.f_bw)
        self.f_remote = QLabel()
        lay.addWidget(self.f_remote)
        self.f_sched = QLabel()
        lay.addWidget(self.f_sched)
        self.f_sec = QLabel()
        lay.addWidget(self.f_sec)
        ver = QLabel(f"v{VERSION}")
        lay.addWidget(ver)
        return f

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        for text, cb in (("Open PyDM", self.show_window), ("New download…", self.open_new),
                         ("Download video…", self.open_video),
                         (None, None), ("Resume all", self.m.resume_all),
                         ("Pause all", self.m.pause_all), (None, None), ("Quit", self.quit)):
            if text is None:
                menu.addSeparator()
            else:
                a = QAction(text, menu)
                a.triggered.connect(lambda _=False, cb=cb: cb())
                menu.addAction(a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_window() if r in (QSystemTrayIcon.Trigger,
                                                  QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def _shortcuts(self):
        for seq, cb in ((QKeySequence.New, self.open_new), ("Ctrl+B", self.open_batch),
                        ("Ctrl+Shift+V", self.open_video), ("Ctrl+G", self.open_grabber),
                        (QKeySequence.Find, self.search.setFocus),
                        ("Ctrl+,", self.open_settings), (QKeySequence.Delete, self._delete_key),
                        ("Space", self._toggle_selected), ("Return", self._open_selected),
                        ("Ctrl+Q", self.quit)):
            QShortcut(QKeySequence(seq), self, activated=cb)
        paste = QShortcut(QKeySequence.Paste, self.list)
        paste.setContext(Qt.WidgetShortcut)
        paste.activated.connect(self._paste_link)

    # ------------------------------------------------------------------ state
    def selected(self):
        return [i.data(Qt.UserRole) for i in self.list.selectionModel().selectedRows()]

    def set_view(self, key):
        self.view = key
        for k, b in self.nav.items():
            b.setChecked(k == key)
        self.proxy.view = key
        self.proxy.invalidateFilter()
        if key == "stats":
            self.stats.refresh()
        self.tick()

    def _search(self, text):
        self.proxy.text = text.strip().lower()
        self.proxy.invalidateFilter()
        self.tick()

    def _selection_changed(self, *_):
        sel = self.selected()
        self.details.show_download(sel[0] if sel else None)

    def tick(self):
        if self._update_found:
            man, self._update_found = self._update_found, None
            self._offer_update(man)
        self._drain_bridge()
        self._drain_events()
        self.m.schedule()
        sel_ids = {d.id for d in self.selected()}
        current = self.details.d
        reset = self.model.refresh()
        self.proxy.invalidateFilter()
        if reset and sel_ids:
            sm = self.list.selectionModel()
            for row in range(self.proxy.rowCount()):
                idx = self.proxy.index(row, 0)
                if idx.data(Qt.UserRole).id in sel_ids:
                    sm.select(idx, QItemSelectionModel.Select)
        if current and current not in self.model.items:
            self.details.show_download(None)
        items = self.model.items
        for key, b in self.nav.items():
            b.set_count(0 if key == "stats" else sum(1 for d in items if matches_view(d, key)))
        titles = dict((k, t) for k, t, _ in VIEWS)
        self.title.setText(titles.get(self.view, self.view[4:] if self.view.startswith("cat:")
                                      else ""))
        running = [d for d in items if d.is_running()]
        total_speed = sum(d.speed for d in running)
        shown = self.proxy.rowCount()
        self.subtitle.setText(f"{shown} item{'s' if shown != 1 else ''}"
                              + (f"  ·  {len(running)} downloading" if running else ""))
        if self.view == "stats":
            self.list_stack.setCurrentIndex(2)
            self.subtitle.setText("Your download history")
        else:
            self.list_stack.setCurrentIndex(0 if shown else 1)
        if not shown:
            self.empty_title.setText("Nothing here yet" if not self.proxy.text
                                     else "No matches")
        self.list.viewport().update()
        self.details.update_view()

        self.sleep.set(self.m.settings["prevent_sleep"] and bool(running))
        self.global_history.append(total_speed)
        self.side_speed.setText(f"{human_size(total_speed)}/s")
        self.side_spark.set_values(self.global_history)
        lim = self.m.settings["speed_limit_kbps"]
        self.limit_btn.setText(f"  Limit: {lim} KB/s" if lim else "  No speed limit")

        queued = sum(1 for d in items if d.status == "Queued" and not d.is_running())
        self.f_stats.setText(f"↓ {human_size(total_speed)}/s     {len(running)} active · "
                             f"{queued} queued · {len(items)} total")
        if self.bridge and not self.bridge_error:
            live = time.time() - self.bridge.last_contact < 120
            self.f_dot.setStyleSheet(f"color: {C['success'] if live else C['accent2']};"
                                     " font-size: 9px;")
            self.f_browser.setText("Browser connected" if live else "Browser integration ready")
        else:
            self.f_dot.setStyleSheet(f"color: {C['warn']}; font-size: 9px;")
            self.f_browser.setText("Browser integration off")
        s = self.m.settings
        self.f_sched.setText(f"⏱ Scheduled {s['scheduler_start']}–{s['scheduler_stop']}"
                             if s["scheduler_enabled"] else "")
        reason = self.m.bandwidth_reason
        self.f_bw.setText(f"⚡ Limited: {reason} ({human_size(self.m.limiter.rate)}/s)"
                          if reason and self.m.limiter.rate else "")
        self.f_remote.setText("📱 Remote on" if self.remote_srv else "")
        threats = sum(1 for d in items if (d.security or {}).get("level") == "danger")
        self.f_sec.setText(f"⚠ {threats} threat{'s' if threats != 1 else ''} found" if threats
                           else "🛡 Protected")
        self.f_sec.setStyleSheet(f"color: {C['danger'] if threats else C['muted']};")
        if self.tray:
            self.tray.setToolTip(f"{APP_NAME} — {len(running)} active · "
                                 f"{human_size(total_speed)}/s")

    # ------------------------------------------------------------------ inputs
    def _modal_open(self):
        return QApplication.activeModalWidget() is not None

    def _drain_bridge(self):
        if not self.bridge or self._modal_open():
            return
        try:
            origin = self.bridge.pair_requests.get_nowait()
        except Exception:  # noqa: BLE001
            origin = None
        if origin:
            self.show_window()
            ok = PairingDialog(self, origin).exec() == PairingDialog.Accepted
            self.bridge.resolve_pairing(origin, ok)
            if ok:
                self.toasts.show("Extension approved", "Downloads from your browser will now "
                                                       "come to PyDM.", "safe")
            return
        try:
            item = self.bridge.inbox.get_nowait()
        except Exception:  # noqa: BLE001
            return
        kind = item.get("type", "add")
        if kind == "media":
            self.show_window()
            VideoDialog(self, self.m, item["page_url"], item.get("candidates"),
                        item.get("cookies"), item.get("referrer"), item.get("title"),
                        "browser").exec()
            return
        if kind == "batch":
            self.show_window()
            GrabberDialog(self, self.m, links=item["links"], referrer=item.get("referrer")).exec()
            return
        if self.m.settings["confirm_browser_downloads"]:
            self.show_window()
            self.open_new(item["url"], item.get("referrer"), item.get("cookies"),
                          item.get("filename"), "browser", item.get("origin"))
        else:
            d = self.m.add(item["url"], referrer=item.get("referrer"),
                           cookies=item.get("cookies"), source="browser")
            self.toasts.show("Download added from browser", d.filename, "info")

    def _drain_events(self):
        while True:
            try:
                kind, d = self.m.events.get_nowait()
            except Exception:  # noqa: BLE001
                return
            self._phone(kind, d)
            if kind == "completed" and d.source == "update":
                self._update_ready(d)
                continue
            if kind == "completed":
                lvl = (d.security or {}).get("level")
                self.notify("Download complete", d.filename,
                            "caution" if lvl == "caution" else "safe",
                            [("Open", lambda d=d: self.open_file(d)),
                             ("Show in folder", lambda d=d: self.reveal(d))])
            elif kind == "threat":
                self.notify("Potential threat detected", f"{d.filename} — review the security "
                                                         "report before opening.", "danger",
                            [("Review", lambda d=d: self.focus_download(d))])
            elif kind in ("failed", "blocked"):
                self.notify("Download blocked" if kind == "blocked" else "Download failed",
                            f"{d.filename}: {d.error}", "danger" if kind == "blocked"
                            else "caution",
                            [("Retry", lambda d=d: self.m.resume(d))] if kind == "failed"
                            else None)
            elif kind == "schedule_start":
                self.notify("Scheduled downloads started", "", "info")
            elif kind == "schedule_stop":
                self.notify("Scheduled downloads paused", "They'll continue at "
                            f"{self.m.settings['scheduler_start']}.", "info")
            elif kind == "queue_done":
                if self.m.settings["on_queue_done"] == "quit":
                    QTimer.singleShot(3000, self.quit)
                    self.notify("Queue finished", "PyDM will quit in a moment.", "info")

    def _phone(self, kind, d):
        s = self.m.settings
        if not (s["ntfy_enabled"] and s["ntfy_topic"]) or d is None:
            return
        msgs = {"completed": ("Download complete", "white_check_mark", "default"),
                "threat": ("Threat detected", "warning", "high"),
                "failed": ("Download failed", "x", "default"),
                "blocked": ("Download blocked", "no_entry", "default")}
        if kind in msgs:
            title, tag, prio = msgs[kind]
            body = d.filename + (f" · {human_size(d.total)}" if kind == "completed" and d.total
                                 else (f" — {d.error}" if d.error else ""))
            notify.send_async(s["ntfy_server"], s["ntfy_topic"], title, body, prio, tag,
                              self.m.opener)

    # ------------------------------------------------------------------ remote & updates
    def apply_remote(self):
        s = self.m.settings
        want = (s["remote_enabled"], s["remote_lan"], s["remote_port"], s["remote_token"])
        if getattr(self, "_remote_cfg", None) == want:
            return
        if self.remote_srv:
            self.remote_srv.stop()
            self.remote_srv = None
        self._remote_cfg = want
        if not s["remote_enabled"]:
            return
        if not s["remote_token"]:
            self.m.update_settings({"remote_token": remote.new_token()})
        try:
            self.remote_srv = remote.RemoteServer(self.m, int(s["remote_port"]), s["remote_lan"],
                                                  self.m.settings["remote_token"])
            self.remote_srv.start()
        except OSError as e:
            self.remote_srv = None
            self.toasts.show("Remote control couldn't start", str(e), "caution")

    def check_updates(self, quiet=True):
        from .. import release
        s = self.m.settings
        url = s["update_url"] or release.UPDATE_URL
        key = s["update_public_key"] or release.PUBLIC_KEY
        if not (url and key):
            return

        def work():
            try:
                man = updater.check(url, key, self.m.opener)
            except Exception:  # noqa: BLE001
                man = None
            if man:
                self._update_found = man
        threading.Thread(target=work, daemon=True).start()

    def _offer_update(self, man):
        def get():
            d = self.m.add(man["url"], checksum=("sha256", man["sha256"]), source="update",
                           save_dir=os.path.join(os.path.expanduser("~"), ".pydm", "updates"))
            self.toasts.show("Downloading update", f"PyDM {man['version']}", "info")
            return d
        self.toasts.show(f"PyDM {man['version']} is available", man.get("notes", "")[:140],
                         "info", [("Download update", get), ("Later", lambda: None)])

    def _update_ready(self, d):
        sec = d.security or {}
        if sec.get("level") == "danger" or not any("Checksum verified" in f["text"]
                                                   for f in sec.get("findings", [])):
            self.toasts.show("Update rejected", "The installer didn't match the signed "
                                                "checksum and was not opened.", "danger")
            return
        self.toasts.show("Update ready", "The installer is verified.", "safe",
                         [("Install now", lambda: open_path(d.path)), ("Later", lambda: None)])

    def notify(self, title, text, level="info", actions=None):
        if not self.m.settings["notifications"]:
            return
        if self.isActiveWindow() or not self.tray:
            self.toasts.show(title, text, level, actions)
        else:
            ic = {"danger": QSystemTrayIcon.Critical, "caution": QSystemTrayIcon.Warning}
            self.tray.showMessage(title, text or APP_NAME, ic.get(level, QSystemTrayIcon.Information),
                                  6000)
            self.toasts.show(title, text, level, actions)

    def _clipboard_changed(self):
        text = QApplication.clipboard().text().strip()
        if not text or text == self._last_clip:
            return
        self._last_clip = text
        if (text == self._own_clip or not self.m.settings["monitor_clipboard"]
                or "\n" in text or not is_http_url(text) or self.m.find_url(text)):
            return
        if ext_of(text.split("?")[0]) not in DOWNLOAD_EXTS:
            return
        name = text.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        self.toasts.show("Download link detected", name, "info",
                         [("Download", lambda: self.open_new(text, source="clipboard")),
                          ("Dismiss", lambda: None)])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        urls = [u.toString() for u in md.urls()] if md.hasUrls() else md.text().split()
        urls = [u for u in urls if is_http_url(u)]
        if len(urls) == 1:
            self.open_new(urls[0])
        elif urls:
            self.open_batch("\n".join(urls))

    def _paste_link(self):
        text = QApplication.clipboard().text().strip()
        urls = [u for u in text.split() if is_http_url(u)]
        if len(urls) > 1:
            self.open_batch("\n".join(urls))
        else:
            self.open_new(urls[0] if urls else "")

    # ------------------------------------------------------------------ actions
    def open_new(self, url="", referrer=None, cookies=None, filename=None, source="manual",
                 origin=None):
        if not url:
            clip = QApplication.clipboard().text().strip()
            if is_http_url(clip) and "\n" not in clip:
                url = clip
        NewDownloadDialog(self, self.m, url, referrer, cookies, filename, source, origin).exec()
        self.tick()

    def open_batch(self, text=""):
        BatchDialog(self, self.m, text).exec()
        self.tick()

    def open_settings(self):
        SettingsDialog(self, self.m, self.bridge, self.bridge_error).exec()
        self.apply_remote()
        self.tick()

    def open_video(self, url=""):
        if not url:
            clip = QApplication.clipboard().text().strip()
            url = clip if is_http_url(clip) and "\n" not in clip else ""
        VideoDialog(self, self.m, url).exec()
        self.tick()

    def open_grabber(self, url=""):
        GrabberDialog(self, self.m, url).exec()
        self.tick()

    def _limit_menu(self):
        menu = QMenu(self)
        cur = self.m.settings["speed_limit_kbps"]
        for label, val in (("No limit", 0), ("256 KB/s", 256), ("512 KB/s", 512),
                           ("1 MB/s", 1024), ("2 MB/s", 2048), ("5 MB/s", 5120),
                           ("10 MB/s", 10240)):
            a = menu.addAction(("✓  " if val == cur else "     ") + label)
            a.triggered.connect(lambda _=False, v=val: self.m.update_settings(
                {"speed_limit_kbps": v}))
        menu.addSeparator()
        custom = menu.addAction("     Custom…")
        custom.triggered.connect(self._custom_limit)
        menu.exec(self.limit_btn.mapToGlobal(self.limit_btn.rect().bottomLeft()))
        self.tick()

    def _custom_limit(self):
        v, ok = QInputDialog.getInt(self, "Speed limit", "Global limit in KB/s (0 = none):",
                                    self.m.settings["speed_limit_kbps"], 0, 10_000_000, 128)
        if ok:
            self.m.update_settings({"speed_limit_kbps": v})

    def _activate(self, d):
        if d.status == "Completed":
            self.open_file(d)
        elif d.is_running() or d.status == "Queued":
            self.m.pause(d)
        else:
            self.m.resume(d)

    def _open_selected(self):
        if self.list.hasFocus():
            for d in self.selected()[:1]:
                self._activate(d)

    def _toggle_selected(self):
        if not self.list.hasFocus():
            return
        for d in self.selected():
            if d.is_running() or d.status == "Queued":
                self.m.pause(d)
            elif d.status in ("Paused", "Error"):
                self.m.resume(d)

    def _delete_key(self):
        if self.list.hasFocus():
            self.remove(self.selected())

    def open_file(self, d):
        if not d or d.status != "Completed" or not d.path or not os.path.exists(d.path):
            self.toasts.show("File not found", "It may have been moved or deleted.", "caution")
            return
        if self.m.settings["confirm_risky_open"] and security.is_risky_to_open(d.security,
                                                                               d.filename):
            sec = d.security or {}
            danger = sec.get("level") == "danger"
            findings = [f for f in sec.get("findings", []) if f["level"] != "info"] or [
                {"level": "caution", "text": "This is a program. It can make changes to your "
                                             "computer."}]
            if not ConfirmDialog.ask(self, "Open this file?",
                                     f"“{d.filename}” " + ("has serious warning signs."
                                                           if danger else "needs your review."),
                                     "danger" if danger else "caution", findings,
                                     "Open anyway" if danger else "Open",
                                     danger=danger):
                return
        open_path(d.path)

    def reveal(self, d):
        if d:
            reveal_in_folder(d.path if d.path and os.path.exists(d.path)
                             else (d.save_dir or self.m.settings["download_dir"]))

    def refresh_address(self, d):
        if not d:
            return
        url, ok = QInputDialog.getText(self, "Refresh download address",
                                       "Paste a new link for the same file.\nYour progress is "
                                       "kept if it's the same file:", text=d.url)
        if ok and is_http_url(url):
            try:
                self.m.refresh_url(d, url)
                self.m.resume(d)
                self.toasts.show("Address updated", "Resuming where it left off.", "safe")
            except ValueError as e:
                self.toasts.show("Can't change address", str(e), "caution")

    def remove(self, items):
        items = [d for d in items if d]
        if not items:
            return
        n = len(items)
        res = ConfirmDialog.ask(self, f"Remove {n} download{'s' if n > 1 else ''}?",
                                "They'll be removed from the list.", "caution", None, "Remove",
                                danger=True, checkbox="Also delete the files from disk")
        ok, delete = res
        if ok:
            for d in items:
                self.m.remove(d, delete_files=delete)
            self.tick()

    def clear_completed(self):
        n = sum(1 for d in self.model.items if d.status == "Completed")
        if n:
            self.m.clear_completed()
            self.toasts.show(f"Cleared {n} completed", "Files stay on your disk.", "info")
            self.tick()

    def copy_text(self, text, msg="Copied"):
        if text:
            self._own_clip = text
            QApplication.clipboard().setText(text)
            self.toasts.show(msg, "", "safe", timeout=2500)

    def focus_download(self, d):
        self.show_window()
        self.set_view("all")
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            if idx.data(Qt.UserRole) is d:
                self.list.setCurrentIndex(idx)
                self.list.scrollTo(idx)

    def _context_menu(self, pos):
        idx = self.list.indexAt(pos)
        if not idx.isValid():
            return
        if not self.list.selectionModel().isSelected(idx):
            self.list.setCurrentIndex(idx)
        sel = self.selected()
        d = sel[0]
        menu = QMenu(self)

        def add(text, ic, cb, enabled=True):
            a = menu.addAction(icon(ic, C["muted"], 15), text)
            a.setEnabled(enabled)
            a.triggered.connect(cb)
        add("Open", "external", lambda: self.open_file(d), d.status == "Completed")
        add("Show in folder", "folder", lambda: self.reveal(d))
        if d.extracted_to:
            add("Open extracted folder", "archive", lambda: reveal_in_folder(d.extracted_to))
        menu.addSeparator()
        add("Resume", "play", lambda: [self.m.resume(x) for x in sel],
            any(x.status in ("Paused", "Error") for x in sel))
        add("Pause", "pause", lambda: [self.m.pause(x) for x in sel],
            any(x.is_running() or x.status == "Queued" for x in sel))
        add("New address…", "refresh", lambda: self.refresh_address(d),
            d.status in ("Paused", "Error"))
        add("Move up in queue", "arrow-up", lambda: self.m.move(d, -1))
        add("Move down in queue", "arrow-down", lambda: self.m.move(d, 1))
        menu.addSeparator()
        add("Copy address", "link", lambda: self.copy_text(d.url, "Address copied"))
        add("Copy SHA-256", "copy", lambda: self.copy_text(d.sha256, "SHA-256 copied"),
            bool(d.sha256))
        menu.addSeparator()
        add("Remove…", "trash", lambda: self.remove(sel))
        menu.exec(self.list.viewport().mapToGlobal(pos))
        self.tick()

    # ------------------------------------------------------------------ window
    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.details.setVisible(self.width() >= 1240)

    def closeEvent(self, e):
        if not self._quitting and self.tray and self.m.settings["close_to_tray"]:
            e.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "PyDM keeps running in the tray. Right-click "
                                                "the icon to quit.", QSystemTrayIcon.Information,
                                      4000)
            return
        if not self._quitting and any(d.is_running() for d in self.m.downloads):
            if not ConfirmDialog.ask(self, "Quit PyDM?", "Active downloads will pause and "
                                                         "continue next time you open PyDM.",
                                     "caution", None, "Quit"):
                e.ignore()
                return
        QSettings(APP_NAME, APP_NAME).setValue("geometry", self.saveGeometry())
        self.sleep.set(False)
        if self.remote_srv:
            self.remote_srv.stop()
        if self.tray:
            self.tray.hide()
        e.accept()
        QApplication.quit()

    def quit(self):
        self._quitting = True
        self.close()
