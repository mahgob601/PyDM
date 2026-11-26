"""Instant preview: watch or listen to a file while it is still downloading.

A small HTTP server on 127.0.0.1 serves a download's partly written file with Range support.
When a player asks for bytes that aren't on disk yet (it seeked, or an MP4 keeps its index at
the end), the engine is told to fetch from that spot right away (Download.prioritize) and the
request waits for the data instead of failing.

Only this PC can connect (127.0.0.1), and every URL carries a random per-session secret.
"""
import hmac
import html
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .log import log

BROWSER_PLAYABLE = {".mp4", ".m4v", ".webm", ".mov", ".mp3", ".m4a", ".aac", ".ogg", ".oga",
                    ".ogv", ".opus", ".wav", ".flac"}
MEDIA_EXTS = BROWSER_PLAYABLE | {".mkv", ".avi", ".wmv", ".flv", ".ts", ".m2ts", ".mpg",
                                 ".mpeg", ".3gp", ".wma"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac", ".wma"}
CHUNK = 256 * 1024
WAIT_FOR_DATA = 90          # seconds a request waits for bytes that are on their way


def ext(name):
    return os.path.splitext(name or "")[1].lower()


def can_preview(d):
    """Direct video/audio files whose size is known and that support ranges."""
    return (d.kind == "file" and ext(d.filename) in MEDIA_EXTS and
            (d.status == "Completed" or (d.total and d.resumable)))


def vlc_path():
    for p in (os.path.expandvars(r"%ProgramFiles%\VideoLAN\VLC\vlc.exe"),
              os.path.expandvars(r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe"),
              "/Applications/VLC.app/Contents/MacOS/VLC"):
        if os.path.isfile(p):
            return p
    return shutil.which("vlc")


class PreviewServer:
    def __init__(self, manager):
        self.m = manager
        self.secret = secrets.token_urlsafe(18)
        self.server = None
        self.lock = threading.Lock()

    # --------------------------------------------------------------------- public
    def ensure(self):
        with self.lock:
            if self.server is None:
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
                self.server.daemon_threads = True
                threading.Thread(target=self.server.serve_forever, name="preview",
                                 daemon=True).start()
                log.info("preview server on 127.0.0.1:%d", self.server.server_address[1])
        return self.server.server_address[1]

    def stream_url(self, d):
        port = self.ensure()
        return f"http://127.0.0.1:{port}/{self.secret}/{d.id}/" + urllib.parse.quote(
            d.filename or "file")

    def player_url(self, d):
        port = self.ensure()
        return f"http://127.0.0.1:{port}/{self.secret}/{d.id}/~player"

    def open(self, d):
        """Start playback in the best available player. Returns what was used."""
        if d.status != "Completed" and not d.is_running():
            self.m.resume(d)                       # previewing a paused download resumes it
        e = ext(d.filename)
        vlc = vlc_path()
        if e not in BROWSER_PLAYABLE and vlc:
            subprocess.Popen([vlc, self.stream_url(d)], close_fds=True)
            return "vlc"
        import webbrowser
        webbrowser.open(self.player_url(d))
        return "browser"

    def stop(self):
        with self.lock:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
                self.server = None

    # --------------------------------------------------------------------- serving
    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _fail(self, code, msg=""):
                body = msg.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _download(self):
                parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/", 2)
                if len(parts) < 3 or not hmac.compare_digest(parts[0], srv.secret):
                    return None, None
                return srv.m.get(parts[1]), urllib.parse.unquote(parts[2])

            def do_HEAD(self):
                self.do_GET(head=True)

            def do_GET(self, head=False):
                d, rest = self._download()
                if d is None:
                    return self._fail(404, "Not found")
                if rest == "~player":
                    return self._player(d)
                self._stream(d, head)

            def _player(self, d):
                name = d.filename or "file"
                tag = "audio" if ext(name) in AUDIO_EXTS else "video"
                src = urllib.parse.quote(name)
                page = PLAYER.format(title=html.escape(name), tag=tag, src=src,
                                     vlc=("" if ext(name) in BROWSER_PLAYABLE else
                                          "<p class=warn>Your browser may not play this "
                                          "format. Install VLC and PyDM will use it.</p>"))
                body = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; media-src 'self'")
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, d, head):
                total = d.total or 0
                if not total:
                    return self._fail(409, "Size unknown: preview isn't possible for this one")
                rng = self.headers.get("Range", "")
                start, end = 0, total - 1
                partial = False
                if rng.startswith("bytes="):
                    a, _, b = rng[6:].split(",")[0].partition("-")
                    try:
                        if a:
                            start = int(a)
                            end = min(int(b), total - 1) if b else total - 1
                        else:                                 # suffix: last N bytes
                            start = max(0, total - int(b))
                        partial = True
                    except ValueError:
                        return self._fail(416)
                    if start > end or start >= total:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                ctype = mimetypes.guess_type(d.filename or "")[0] or "application/octet-stream"
                if ctype == "video/quicktime":
                    ctype = "video/mp4"                       # Chrome plays H.264 .mov as mp4
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if head:
                    return
                pos = start
                f = None
                try:
                    while pos <= end:
                        n = d.available_from(pos)
                        if n <= 0:
                            d.prioritize(pos)
                            deadline = time.time() + WAIT_FOR_DATA
                            while n <= 0 and time.time() < deadline:
                                if d.status in ("Error",) and not d.retry_at:
                                    return
                                if srv.m.get(d.id) is None:
                                    return                    # removed
                                time.sleep(0.05)
                                n = d.available_from(pos)
                            if n <= 0:
                                return                        # player will retry the range
                        path = d.path if d.status == "Completed" and d.path and \
                            os.path.exists(d.path) else d.temp_path
                        if f is None or f.name != path:
                            if f:
                                f.close()
                            f = open(path, "rb")
                        f.seek(pos)
                        data = f.read(min(n, CHUNK, end - pos + 1))
                        if not data:
                            time.sleep(0.05)
                            continue
                        self.wfile.write(data)
                        pos += len(data)
                except (ConnectionError, OSError):
                    pass                                      # player closed or seeked away
                finally:
                    if f:
                        f.close()

        return Handler


PLAYER = """<!doctype html><html><head><meta charset="utf-8"><title>▶ {title} — PyDM preview</title>
<style>
html,body{{margin:0;height:100%;background:#0b0d12;color:#e6e8ef;
  font:14px "Segoe UI",system-ui,sans-serif}}
main{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:14px;padding:20px;box-sizing:border-box}}
video{{max-width:100%;max-height:82vh;border-radius:12px;background:#000;
  box-shadow:0 10px 40px #0008}}
audio{{width:min(640px,90vw)}}
h1{{font-size:15px;font-weight:600;margin:0;opacity:.85;text-align:center;word-break:break-all}}
p{{margin:0;color:#8b90a0;font-size:13px}} .warn{{color:#f5a524}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#7c5cff;
  margin-right:6px;animation:p 1.2s infinite}} @keyframes p{{50%{{opacity:.3}}}}
</style></head><body><main>
<h1>{title}</h1>
<{tag} id=m src="{src}" controls autoplay preload="auto"></{tag}>
<p id=s><span class=dot></span>Playing while PyDM downloads. Jump anywhere: PyDM fetches that
part first.</p>{vlc}
<script>
const m=document.getElementById('m'),s=document.getElementById('s');
m.addEventListener('waiting',()=>s.innerHTML='<span class=dot></span>Fetching this part…');
m.addEventListener('playing',()=>s.innerHTML='<span class=dot></span>Playing while PyDM downloads.');
m.addEventListener('error',()=>s.innerHTML='<span class=warn>Your browser can’t play this file’s format. Install VLC and use Preview again.</span>');
</script></main></body></html>"""
