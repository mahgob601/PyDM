"""Opens the real PyDM window and dialogs, saves screenshots, checks Windows UI integration.
Usage: gui_harness.py VID_URL VID_SLOW_URL OUT_DIR HOME_DIR   (prints a JSON summary last)"""
import json
import os
import sys

vid, slow, out, home = sys.argv[1:5]
os.environ["USERPROFILE"] = os.environ["HOME"] = home
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget  # noqa: E402

app = QApplication(sys.argv)
from pydm.core.engine import Manager  # noqa: E402
from pydm.ui import dialogs, extra  # noqa: E402
from pydm.ui.main_window import MainWindow  # noqa: E402
from pydm.ui.theme import QSS  # noqa: E402

app.setStyle("Fusion")
f = QFont()
f.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "Arial"])
f.setPointSizeF(10)
app.setFont(f)
app.setStyleSheet(QSS)
checks, shots = {}, [0]
m = Manager()
m.update_settings({"download_dir": os.path.join(home, "Downloads"), "notifications": False,
                   "max_active": 5, "antivirus_scan": False, "check_updates": False,
                   "speed_rules": [{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00",
                                    "kbps": 1024}], "ntfy_topic": "pydm-test-topic"})
m.add_stream(slow + "master/master.m3u8", title="Conference keynote 2026")
m.add_stream(slow + "hls_plain/index.m3u8", title="Product demo")
m.add(vid + "files/a.zip")
m.add(vid + "docs/manual.pdf")
m.add(vid + "f100.bin")
w = MainWindow(m, None, "test")
w.resize(1400, 880)
w.show()
IS_WIN = sys.platform.startswith("win")
if IS_WIN:
    checks["system tray available"] = [QSystemTrayIcon.isSystemTrayAvailable(), ""]


def shot(widget, name):
    widget.grab().save(os.path.join(out, f"ui_{name}.png"))
    shots[0] += 1


def sel(prefix):
    for row in range(w.proxy.rowCount()):
        idx = w.proxy.index(row, 0)
        if idx.data(Qt.UserRole).filename.startswith(prefix):
            w.list.setCurrentIndex(idx)


def s1():
    sel("Conference")
    w.tick()
    app.processEvents()
    shot(w, "main")
    # Worst case: a small laptop screen. The sidebar speed card must keep its full height.
    w.resize(960, 600)
    app.processEvents()
    sp = w.side_spark
    card = sp.parentWidget()
    visible = card.visibleRegion().boundingRect().height() >= card.sizeHint().height() - 2
    checks["sidebar speed card intact on a 960x600 window"] = [
        sp.height() >= 30 and card.height() >= card.sizeHint().height() - 2 and visible,
        f"spark {sp.height()}px, card {card.height()}/{card.sizeHint().height()}px"]
    shot(w, "main_small")
    w.resize(1400, 880)
    app.processEvents()
    if IS_WIN:
        checks["tray icon + notification work"] = [bool(w.tray), "tray icon created"]
    if w.tray:
        w.tray.showMessage("PyDM test", "Notification test", QSystemTrayIcon.Information, 2000)


def s2():
    w.set_view("stats")
    app.processEvents()
    shot(w, "stats")
    w.set_view("all")


def s3():
    dlg = extra.VideoDialog(w, m, vid + "page.html",
                            [{"url": vid + "master/master.m3u8", "type": "hls"}],
                            title="Test Video Page")
    dlg.show()

    def done():
        checks["video dialog finds qualities"] = [len(dlg.options) >= 3,
                                                  f"{len(dlg.options)} options"]
        shot(dlg, "video")
        dlg.close()
    QTimer.singleShot(6000, done)


def s4():
    g = extra.GrabberDialog(w, m, vid + "page.html")
    g.depth.setValue(2)
    g.show()
    QTimer.singleShot(100, g.scan)

    def done():
        checks["grabber dialog lists files"] = [len(g.items) >= 5, f"{len(g.items)} files"]
        shot(g, "grabber")
        g.close()
    QTimer.singleShot(4000, done)


def s5():
    s = dialogs.SettingsDialog(w, m, None, None)
    s.show()
    pages = [(1, "speed"), (2, "bandwidth"), (5, "security"), (6, "video"), (9, "remote")]

    def fits():
        from PySide6.QtWidgets import QScrollArea
        bad = []
        for k in range(s.stack.count()):
            sc = s.stack.widget(k)
            if isinstance(sc, QScrollArea) and sc.widget() is not None:
                need = sc.widget().minimumSizeHint().width()
                have = sc.viewport().width()
                if need > have + 1:
                    bad.append(f"page {k}: needs {need}px, has {have}px")
        return bad

    def nxt(i=0):
        if i >= len(pages):
            bad = fits()
            checks["settings pages fit without sideways scrolling"] = [
                not bad, "; ".join(bad) or f"{s.width()}x{s.height()}"]
            s.close()
            return
        s.nav.setCurrentRow(pages[i][0])
        app.processEvents()
        QTimer.singleShot(300, lambda: (shot(s, "settings_" + pages[i][1]), nxt(i + 1)))
    nxt()


def s6():
    nd = dialogs.NewDownloadDialog(w, m, vid + "files/b.zip")
    nd.show()
    QTimer.singleShot(2000, lambda: (shot(nd, "new_download"), nd.close()))


def s7():
    if not sys.platform.startswith("win"):
        return
    from pydm.core.bandwidth import fullscreen_busy
    before = fullscreen_busy()
    fs = QWidget()
    fs.setStyleSheet("background:black")
    fs.showFullScreen()
    fs.raise_()
    fs.activateWindow()

    def measure():
        during = fullscreen_busy()
        fs.close()
        checks["full-screen app detected (quiet mode trigger)"] = [
            (not before) and during, f"before={before} during={during}"]
    QTimer.singleShot(2500, measure)


def finish():
    m.shutdown()
    print(json.dumps({"shots": shots[0], "checks": checks}))
    sys.stdout.flush()
    app.quit()


for ms, fn in [(6000, s1), (7000, s2), (7500, s3), (14500, s4), (19500, s5), (23000, s6),
               (26000, s7), (30000, finish)]:
    QTimer.singleShot(ms, fn)
app.exec()
