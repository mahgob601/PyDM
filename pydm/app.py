"""Application entry point: GUI by default, --cli for terminal downloads."""
import argparse
import os
import sys
import time

from .core.util import APP_DIR, APP_NAME, VERSION, human_size, human_time


def run_cli(url, out_dir, connections, name, checksum, mirrors=None, sequential=False,
            extract=False, video=False, quality=None, clip=None):
    from .core.engine import Manager
    from .core.power import SleepInhibitor
    m = Manager(persist=False)
    m.update_settings({"use_categories": False, "download_dir": out_dir,
                       "antivirus_scan": False, "auto_retry_limit": 5})
    algo_value = None
    if checksum:
        algo, _, value = checksum.partition(":")
        algo_value = (algo.lower().replace("-", ""), value.lower())
    low = url.lower().split("?")[0]
    if clip:
        from .core import media
        try:
            start, end = media.check_clip(media.parse_time(clip[0]), media.parse_time(clip[1]))
            d = m.add_clip(url, os.path.splitext(name)[0] if name else "clip", start, end,
                           height=quality, save_dir=out_dir, source="cli")
        except media.MediaError as e:
            print(e)
            return 1
    elif video or low.endswith(".m3u8"):
        from .core import media
        if low.endswith(".m3u8"):
            d = m.add_stream(url, title=os.path.splitext(name or "video")[0], height=quality,
                             save_dir=out_dir, source="cli")
        else:
            info = media.resolve(url)
            opts = media.choices(info, bool(media.ffmpeg_path(m.settings)))
            if not opts:
                print("No downloadable video found")
                return 1
            pick = opts[0]
            if quality:
                fits = [o for o in opts if o.get("height") and o["height"] <= quality]
                pick = fits[0] if fits else opts[0]
            if name:
                info["title"] = os.path.splitext(name)[0]
            print(f"{info['title']} — {pick['label']} ({pick['container']})")
            d = m.add_media(info, pick, save_dir=out_dir, source="cli")
    else:
        d = m.add(url, save_dir=out_dir, filename=name, checksum=algo_value, source="cli",
                  connections=connections or None, auto_tune=not connections, mirrors=mirrors,
                  sequential=sequential, extract=extract)
    sleep = SleepInhibitor()
    sleep.set(True)
    m.schedule()
    try:
        while d.is_running() or d.status == "Queued" or (d.status == "Error" and d.retry_at):
            m.schedule()
            if d.status == "Error" and d.retry_at:
                sys.stdout.write(f"\r{d.error[:60]} — retrying in "
                                 f"{max(0, int(d.retry_at - time.time()))}s        ")
                sys.stdout.flush()
                time.sleep(0.5)
                continue
            total, done = d.expected_total, d.downloaded
            pct = f"{done / total * 100:5.1f}%" if total else "      "
            sys.stdout.write(f"\r{d.filename[:36]:<36} {pct} {human_size(done):>9} / "
                             f"{human_size(total):<9} {human_size(d.speed):>9}/s  "
                             f"conns:{d.active_connections:<2} ETA {human_time(d.eta):<8} "
                             f"[{d.status}{' · ' + d.tuning if d.auto_tune and d.tuning else ''}]"
                             "   ")
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        d.stop_event.set()
        if d.thread:
            d.thread.join(timeout=5)
        print("\nStopped.")
        return 1
    finally:
        sleep.set(False)
    print()
    if d.status != "Completed":
        print(f"Failed: {d.error}")
        return 1
    print(f"Saved to {d.path}\nSHA-256 {d.sha256}")
    if d.extracted_to:
        print(f"Extracted to {d.extracted_to}")
    sec = d.security or {}
    for f in sec.get("findings", []):
        print(f"  [{f['level']}] {f['text']}")
    return 2 if sec.get("level") == "danger" else 0


def run_gui(start_hidden=False):
    from PySide6.QtCore import QLockFile, Qt
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtWidgets import QApplication, QMessageBox

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PyDM.DownloadManager")
        except Exception:  # noqa: BLE001
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setQuitOnLastWindowClosed(False)

    from .core.bridge import Bridge
    from .core.engine import Manager
    from .ui.main_window import MainWindow
    from .ui.theme import QSS, app_icon

    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text",
                      "Helvetica Neue", "Noto Sans", "Arial"])
    font.setPointSizeF(10)
    app.setFont(font)
    app.setStyleSheet(QSS)

    os.makedirs(APP_DIR, exist_ok=True)
    if sys.platform.startswith("win"):
        # Lets the installer/updater see that PyDM is running (Inno Setup AppMutex).
        import ctypes
        app._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "PyDM.AppMutex")
    lock = QLockFile(os.path.join(APP_DIR, "pydm.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if not start_hidden:                     # sign-in autostart: just step aside quietly
            QMessageBox.information(None, APP_NAME, "PyDM is already running — look for its "
                                                    "icon in the system tray.")
        return 0

    manager = Manager()
    bridge, bridge_error = None, None
    try:
        bridge = Bridge(int(manager.settings["browser_port"]), manager)
        bridge.start()
    except OSError as e:
        bridge_error = f"port {manager.settings['browser_port']} is in use ({e.strerror})"
        bridge = None

    win = MainWindow(manager, bridge, bridge_error)
    if not start_hidden:
        win.show()
    rc = app.exec()
    manager.shutdown()
    if bridge:
        bridge.stop()
    lock.unlock()
    return rc


def main():
    # A windowed .exe has no console: give print()/argparse somewhere harmless to write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # Windows consoles default to a legacy code page (cp1252/cp437): video titles, file names
    # and our own "—" must never crash a download just because the terminal can't show them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        from .core.log import log, setup
        setup()
        import threading

        def _crash(t, v, tb):
            log.critical("unhandled error", exc_info=(t, v, tb))
            sys.__excepthook__(t, v, tb)
        sys.excepthook = _crash
        threading.excepthook = lambda a: log.critical(
            "unhandled error in %s", a.thread.name if a.thread else "thread",
            exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    except OSError:
        pass                                     # read-only profile: run without a log
    ap = argparse.ArgumentParser(prog="pydm", description=f"{APP_NAME} {VERSION} — "
                                                          "multi-connection download manager")
    ap.add_argument("--cli", metavar="URL", help="download URL in the terminal (no GUI)")
    ap.add_argument("-c", "--connections", type=int, default=0,
                    help="connections (default 0 = auto-tune up to 32)")
    ap.add_argument("-m", "--mirror", action="append", default=[],
                    help="another URL for the same file (repeatable)")
    ap.add_argument("--stream", action="store_true", help="download in order (preview media)")
    ap.add_argument("-x", "--extract", action="store_true", help="extract archive when done")
    ap.add_argument("-v", "--video", action="store_true",
                    help="treat URL as a video page (YouTube etc.; needs yt-dlp)")
    ap.add_argument("-q", "--quality", type=int, help="max video height, e.g. 1080")
    ap.add_argument("--from", dest="clip_from", metavar="TIME",
                    help="only download a video from this time (12:30, 1:02:03 or seconds)")
    ap.add_argument("--to", dest="clip_to", metavar="TIME", help="...until this time")
    ap.add_argument("-o", "--output", default=os.getcwd(), help="output folder (CLI)")
    ap.add_argument("-n", "--name", help="output file name (CLI)")
    ap.add_argument("--checksum", help="verify, e.g. sha256:abc123… (CLI)")
    ap.add_argument("--hidden", action="store_true", help="start minimized to the tray")
    args = ap.parse_args()
    if args.cli:
        sys.exit(run_cli(args.cli, args.output, max(0, min(64, args.connections)), args.name,
                         args.checksum, args.mirror, args.stream, args.extract, args.video,
                         args.quality, (args.clip_from, args.clip_to)
                         if args.clip_from or args.clip_to else None))
    sys.exit(run_gui(args.hidden))
