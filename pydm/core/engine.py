"""Download engine v3 — built for raw speed.

Speed techniques
  * Connection workers with HTTP keep-alive: each worker keeps one TCP/TLS connection and
    reuses it for every piece it downloads (no handshake per piece).
  * Zero-copy hot path: readinto() a pre-allocated buffer, pwrite() straight to the file.
  * 4 MB socket receive buffers for high-bandwidth / high-latency links.
  * Adaptive connection count: starts at 8 and keeps adding connections while each step
    still raises throughput; learns per-host limits when a server pushes back (429/503).
  * Work stealing: a free connection takes over half of the largest remaining piece.
  * Slow-connection replacement: a connection far below the median is re-dialled (often
    lands on a faster CDN node / route).
  * Multi-source: the same file is pulled from several mirrors at once, weighted by speed.
  * Plain pre-allocation. (NTFS sparse files were tried: writes into them measured ~50 MB/s
    against ~2.4 GB/s for a plain file on Windows Server 2025, so they're opt-in only.)
"""
import base64
import collections
import http.client
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import checksums, ftp, media, postprocess, security
from .bandwidth import Governor
from .util import (APP_DIR, DEFAULT_UA, content_disposition_filename, SETTINGS_FILE, STATE_FILE, TEMP_SUFFIX, category_for,
                   describe_error, ext_of, filename_from_url, is_inside, safe_filename,
                   unique_path)

CHUNK_SIZE = 256 * 1024
MIN_SPLIT = 512 * 1024
ENDGAME_SPLIT = 128 * 1024        # smaller pieces allowed when stealing from a slow connection
RCVBUF = 4 * 1024 * 1024
HARD_MAX_CONNECTIONS = 64
AUTO_START = 8
TUNE_INTERVAL = 1.5
STALL_SECONDS = 10            # no bytes on any connection this long: reconnect them all
RUNNING_STATES = ("Connecting", "Downloading", "Verifying", "Extracting", "Merging")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_SECRET_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_FORBIDDEN_HEADERS = {"host", "content-length", "range", "transfer-encoding", "connection",
                      "if-range"}

DEFAULT_SETTINGS = {
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads"),
    "use_categories": True,
    "auto_connections": True,
    "max_connections": 32,
    "connections": 8,
    "max_total_connections": 128,
    "replace_slow_connections": True,
    "max_active": 3,
    "speed_limit_kbps": 0,
    "proxy": "",
    "user_agent": DEFAULT_UA,
    "timeout": 30,
    "max_retries": 8,
    "auto_retry": True,
    "auto_retry_limit": 10,
    "prevent_sleep": True,
    "extract_archives": False,
    "delete_archive_after_extract": False,
    "post_command": "",
    "monitor_clipboard": True,
    "notifications": True,
    "close_to_tray": True,
    "block_downgrade_redirects": True,
    "block_insecure_executables": False,
    "mark_of_the_web": True,
    "sparse_files": False,          # NTFS sparse pre-allocation: opt-in (slow writes)
    "antivirus_scan": True,
    "virustotal_api_key": "",
    "confirm_risky_open": True,
    "scheduler_enabled": False,
    "scheduler_start": "02:00",
    "scheduler_stop": "08:00",
    "scheduler_window_only": False,
    "on_queue_done": "nothing",
    "browser_port": 9614,
    "trusted_origins": [],
    "confirm_browser_downloads": True,
    "host_limits": {},
    "auto_checksum": True,
    "history_enabled": True,
    "speed_rules": [],
    "yield_to_apps": False,
    "quiet_when_fullscreen": True,
    "quiet_kbps": 512,
    "remote_enabled": False,
    "remote_port": 9615,
    "remote_lan": True,
    "remote_token": "",
    "ntfy_enabled": False,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "check_updates": True,
    "update_url": "",
    "update_public_key": "",
    "ffmpeg_path": "",
    "catch_min_kb": 256,
}


class BlockedError(Exception):
    """A security policy or a permanent condition stopped the download (no auto-retry)."""


class FileChanged(BlockedError):
    def __init__(self, msg="The file changed on the server — restart the download"):
        super().__init__(msg)


class IncompleteError(IOError):
    pass


class HTTPStatusError(IOError):
    def __init__(self, code, reason="", retry_after=None):
        super().__init__(f"HTTP {code} {reason}")
        self.code, self.reason = code, reason
        try:
            self.retry_after = float(retry_after) if retry_after else None
        except ValueError:
            self.retry_after = None


def disk_full(e):
    return getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) in (39, 112)


def describe_local_error(e):
    win = getattr(e, "winerror", None)
    if win in (225, 226) or (os.name == "nt" and e.errno == 22):
        # ERROR_VIRUS_INFECTED / ERROR_VIRUS_DELETED; Defender's real-time block shows up as
        # EINVAL when it yanks the file away mid-write.
        return ("Blocked by your antivirus (Windows Security) — the file was flagged as "
                "dangerous and removed")
    if e.errno == 2 or win in (2, 3):
        return ("The file was removed while it was being saved — usually by your antivirus. "
                "Check its quarantine if you trust this file")
    if e.errno in (13, 1) or win == 5:
        return "Can't write to the download folder (access denied) — choose another folder"
    if e.errno == 30:
        return "The download folder is read-only — choose another folder"
    return f"Couldn't write the file: {e.strerror or e}"


def is_transient(e):
    if isinstance(e, (BlockedError, LocalFileError)) or disk_full(e):
        return False
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return code >= 500 or code in (408, 429)
    if isinstance(e, urllib.error.URLError):
        return "CERTIFICATE" not in str(e.reason)
    return isinstance(e, (OSError, http.client.HTTPException))


def parse_headers(text):
    out = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"Invalid header line: {line!r}")
        name, value = (p.strip() for p in line.split(":", 1))
        if not _HEADER_NAME.match(name) or "\r" in value or "\n" in value:
            raise ValueError(f"Invalid header: {name!r}")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"The {name} header is managed by PyDM")
        out[name] = value
    return out


# --------------------------------------------------------------------------- networking
_SSL_CTX = None


def ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()          # certificate + hostname checks always on
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        _SSL_CTX = ctx
    return _SSL_CTX


def check_redirect(old_url, new_url, block_downgrade):
    old, new = urllib.parse.urlsplit(old_url), urllib.parse.urlsplit(new_url)
    if new.scheme not in ("http", "https"):
        raise BlockedError(f"Blocked redirect to a non-web address ({new.scheme}:)")
    if block_downgrade and old.scheme == "https" and new.scheme == "http":
        raise BlockedError("Blocked: the server redirected from secure HTTPS to insecure HTTP "
                           "(Settings → Security to allow)")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 10

    def __init__(self, block_downgrade=True):
        super().__init__()
        self.block_downgrade = block_downgrade

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        full = urllib.parse.urljoin(req.full_url, newurl)
        check_redirect(req.full_url, full, self.block_downgrade)
        r = super().redirect_request(req, fp, code, msg, headers, newurl)
        if r is not None and (urllib.parse.urlsplit(full).hostname
                              != urllib.parse.urlsplit(req.full_url).hostname):
            for h in ("Cookie", "Authorization"):
                r.remove_header(h)
        return r


def build_opener(settings):
    handlers = [urllib.request.HTTPSHandler(context=ssl_context()),
                SafeRedirectHandler(settings.get("block_downgrade_redirects", True))]
    proxy = (settings.get("proxy") or "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def proxy_for(url, settings):
    """(host, port, auth_header) for this URL, or None."""
    host = urllib.parse.urlsplit(url).hostname or ""
    proxy = (settings.get("proxy") or "").strip()
    if not proxy:
        proxies = urllib.request.getproxies()
        proxy = proxies.get(urllib.parse.urlsplit(url).scheme) or proxies.get("all")
        if not proxy or urllib.request.proxy_bypass(host):
            return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    p = urllib.parse.urlsplit(proxy)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    auth = None
    if p.username:
        cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        auth = "Basic " + base64.b64encode(cred.encode()).decode()
    return p.hostname, p.port or 8080, auth


def probe_url(url, headers, opener, timeout=30):
    req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
    with opener.open(req, timeout=timeout) as r:
        final, status, h = r.geturl(), r.status, r.headers
    total, resumable = None, False
    if status == 206:
        m = re.search(r"/(\d+)\s*$", h.get("Content-Range", ""))
        if m:
            total, resumable = int(m.group(1)), True
    else:
        cl = (h.get("Content-Length") or "").strip()
        total = int(cl) if cl.isdigit() else None
    raw = content_disposition_filename(h.get("Content-Disposition")) or filename_from_url(final) or filename_from_url(url)
    name = safe_filename(raw)
    if not os.path.splitext(name)[1]:
        ext = mimetypes.guess_extension(h.get_content_type() or "")
        if ext and ext != ".bin":
            name += ext
    return {"final_url": final, "total": total, "resumable": resumable, "name": name,
            "raw_name": raw, "etag": h.get("ETag"), "last_modified": h.get("Last-Modified"),
            "type": h.get_content_type()}


def validator_of(info):
    etag = info.get("etag")
    if etag and not etag.startswith("W/"):
        return etag
    return info.get("last_modified")


class HttpConn:
    """One persistent (keep-alive) connection owned by a worker."""

    def __init__(self, d):
        self.d = d
        self.conn = None
        self.key = None
        self.via_proxy = False
        self.proxy_auth = None
        self.requests = 0

    def abort(self):
        """Unblock a read stuck on a silent server (called from another thread)."""
        c = self.conn
        sock = getattr(c, "sock", None) if c is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.conn, self.key, self.requests = None, None, 0

    def _open(self, url):
        p = urllib.parse.urlsplit(url)
        host, https = p.hostname, p.scheme == "https"
        port = p.port or (443 if https else 80)
        settings = self.d._settings()
        timeout = settings.get("timeout", 30)
        proxy = proxy_for(url, settings)
        self.via_proxy, self.proxy_auth = False, None
        if proxy:
            phost, pport, auth = proxy
            if https:
                c = http.client.HTTPSConnection(phost, pport, context=ssl_context(),
                                                 timeout=timeout)
                c.set_tunnel(host, port, headers={"Proxy-Authorization": auth} if auth else None)
            else:
                c = http.client.HTTPConnection(phost, pport, timeout=timeout)
                self.via_proxy, self.proxy_auth = True, auth
        elif https:
            c = http.client.HTTPSConnection(host, port, context=ssl_context(), timeout=timeout)
        else:
            c = http.client.HTTPConnection(host, port, timeout=timeout)
        c.connect()
        try:
            c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            pass
        self.conn, self.key, self.requests = c, (p.scheme, host, port), 0

    def get_range(self, url, start, end, validator):
        extra = {"Range": f"bytes={start}-{end}"}
        if validator:
            extra["If-Range"] = validator
        return self.fetch(url, extra)

    def fetch(self, url, extra=None):
        """GET over the kept-alive connection, following redirects. Returns (response, url)."""
        block = self.d._settings().get("block_downgrade_redirects", True)
        for _ in range(8):
            p = urllib.parse.urlsplit(url)
            key = (p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80))
            if self.conn is None or self.key != key:
                self.close()
                self._open(url)
            path = url if self.via_proxy else (p.path or "/") + (f"?{p.query}" if p.query else "")
            hdrs = self.d._headers_for(url, extra)
            if self.proxy_auth:
                hdrs["Proxy-Authorization"] = self.proxy_auth
            for attempt in (0, 1):
                try:
                    self.conn.request("GET", path, headers=hdrs)
                    r = self.conn.getresponse()
                    break
                except (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                        http.client.ResponseNotReady, BrokenPipeError, ConnectionResetError):
                    if attempt or self.requests == 0:
                        raise
                    self.close()                     # stale keep-alive connection: redial
                    self._open(url)
            self.requests += 1
            if r.status in (301, 302, 303, 307, 308):
                loc = r.getheader("Location")
                r.read(65536)
                r.close()
                if not loc:
                    raise IOError("Redirect without a location")
                new = urllib.parse.urljoin(url, loc)
                check_redirect(url, new, block)
                url = new
                continue
            return r, url
        raise IOError("Too many redirects")


class LocalFileError(OSError):
    """Writing to the user's disk failed (not the network): antivirus, permissions, disk."""


def as_local(e):
    """An OSError about a file on disk (it names one) is ours, not the network's: hashing or
    sniffing a file the antivirus just removed fails with EINVAL/ENOENT + the file name."""
    if isinstance(e, LocalFileError):
        return e
    # urllib's HTTPError/URLError are OSErrors too, and HTTPError.filename is the URL.
    if (isinstance(e, OSError) and getattr(e, "filename", None)
            and not isinstance(e, (urllib.error.URLError, ConnectionError, socket.timeout,
                                   socket.gaierror, ssl.SSLError))):
        return local_file_error(e)
    return None


def local_file_error(e):
    err = LocalFileError(e.errno, e.strerror, getattr(e, "filename", None))
    err.winerror = getattr(e, "winerror", None)
    return err


class FileWriter:
    """Positional writes from many threads into one pre-allocated file.

    POSIX uses pwrite on one descriptor. Windows has no pwrite in Python, so each worker
    thread gets its own descriptor (own file position): no shared seek+write lock."""

    def __init__(self, path):
        self.path = path
        self.positional = hasattr(os, "pwrite")
        self.lock = threading.Lock()
        self._local = threading.local()
        self._fds = []
        self.fd = self._open()

    def _open(self):
        try:
            fd = os.open(self.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        except OSError as e:
            raise local_file_error(e) from e
        with self.lock:
            self._fds.append(fd)
        return fd

    def write(self, offset, mv):
        try:
            if self.positional:
                while mv:
                    n = os.pwrite(self.fd, mv, offset)
                    mv, offset = mv[n:], offset + n
                return
            fd = getattr(self._local, "fd", None)
            if fd is None:
                fd = self._local.fd = self._open()
            os.lseek(fd, offset, os.SEEK_SET)
            while mv:
                n = os.write(fd, mv)
                mv = mv[n:]
        except LocalFileError:
            raise
        except OSError as e:
            raise local_file_error(e) from e

    def close(self):
        with self.lock:
            fds, self._fds = self._fds, []
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def make_sparse(path):
    """Windows/NTFS: mark sparse so writing far into the file doesn't zero-fill first."""
    if os.name != "nt":
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        with open(path, "r+b") as f:
            h = msvcrt.get_osfhandle(f.fileno())
            out = wintypes.DWORD()
            ctypes.windll.kernel32.DeviceIoControl(wintypes.HANDLE(h), 0x900C4, None, 0, None,
                                                   0, ctypes.byref(out), None)
    except Exception:  # noqa: BLE001
        pass


class RateLimiter:
    def __init__(self, rate=0):
        self.rate, self.tokens = 0, 0.0
        self.last = time.monotonic()
        self.lock = threading.Lock()
        self.set_rate(rate)

    def set_rate(self, rate):
        with self.lock:
            self.rate = max(0, int(rate or 0))
            self.tokens = 0.0
            self.last = time.monotonic()

    def consume(self, n, stop=None):
        while True:
            with self.lock:
                if self.rate <= 0:
                    return
                now = time.monotonic()
                self.tokens = min(max(self.rate, n), self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            if stop is not None and stop.is_set():
                return
            time.sleep(min(wait, 0.2))


# --------------------------------------------------------------------------- model
class Segment:
    __slots__ = ("start", "end", "done", "active", "failed")

    def __init__(self, start, end, done=0):
        self.start, self.end, self.done = start, end, done
        self.active = self.failed = False

    @property
    def pos(self):
        return self.start + self.done

    def remaining(self):
        return 1 if self.end is None else max(0, self.end - self.pos + 1)


class Source:
    def __init__(self, url, final_url, validator, primary=False):
        self.url, self.final_url, self.validator, self.primary = url, final_url, validator, primary
        self.host = urllib.parse.urlsplit(final_url).hostname or ""
        self.active = 0
        self.bytes = 0
        self.speed = 0.0
        self._last = 0
        self.disabled = False

    def per_conn(self):
        return self.speed / self.active if self.active else self.speed


class Worker:
    _ids = 0

    def __init__(self, source):
        Worker._ids += 1
        self.id = Worker._ids
        self.source = source
        self.alive = True
        self.seg = None
        self.window = 0
        self.speed = 0.0
        self.started = time.monotonic()
        self.restart = False
        self.retire = False
        self.conn = None
        self.restarts = 0
        self.slow_for = 0.0
        self.buf = bytearray(CHUNK_SIZE)
        self.mv = memoryview(self.buf)


class Download:
    PERSIST = ("id", "url", "final_url", "referrer", "auto_dir", "save_dir", "filename",
               "raw_name", "filename_fixed", "category", "path", "total", "resumable", "etag",
               "last_modified", "connections", "auto_tune", "status", "error", "added",
               "finished", "source", "checksum_algo", "checksum_value", "speed_limit_kbps",
               "sha256", "security", "url_refreshed", "mirrors", "sequential", "extract",
               "extracted_to", "retry_at", "auto_retries", "kind", "parent_id", "children",
               "media", "hls", "hls_total", "hls_done", "bytes_done", "elapsed")

    def __init__(self, url, save_dir=None, filename=None, referrer=None, cookies=None,
                 connections=8, manager=None, source="manual", checksum=None,
                 speed_limit_kbps=0, headers=None, auto_tune=True, mirrors=None,
                 sequential=False, extract=False, kind="file", parent_id=None, media=None):
        self.id = uuid.uuid4().hex[:12]
        self.url = url.strip()
        self.final_url = self.url
        self.referrer = referrer
        self.cookies = cookies
        self.headers = dict(headers or {})
        self.auto_dir = save_dir is None
        self.save_dir = save_dir
        self.filename_fixed = bool(filename)
        self.raw_name = filename or filename_from_url(self.url)
        self.filename = safe_filename(self.raw_name)
        self.category = category_for(self.filename)
        self.path = None
        self.total = None
        self.resumable = False
        self.etag = None
        self.last_modified = None
        self.segments = []
        self.connections = max(1, min(HARD_MAX_CONNECTIONS, int(connections)))
        self.auto_tune = auto_tune
        self.status = "Queued"
        self.error = ""
        self.added = time.time()
        self.finished = None
        self.source = source
        self.checksum_algo, self.checksum_value = (checksum or (None, None))
        self.speed_limit_kbps = speed_limit_kbps
        self.sha256 = None
        self.security = None
        self.url_refreshed = False
        self.mirrors = [m.strip() for m in (mirrors or []) if m.strip() and m.strip() != self.url]
        self.sequential = sequential
        self.extract = extract
        self.extracted_to = None
        self.retry_at = None
        self.auto_retries = 0
        self.kind = kind                      # file | hls | media (merge job) | ytdlp
        self.parent_id = parent_id
        self.children = []
        self.media = media                    # page_url, title, format ids, op …
        self.hls = None
        self.hls_total = 0
        self.hls_done = 0
        self.bytes_done = 0
        self.elapsed = 0.0
        self.manager = manager
        # runtime
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.speed = 0.0
        self.history = collections.deque([0.0] * 120, maxlen=120)
        self.limiter = RateLimiter(speed_limit_kbps * 1024)
        self.workers = []
        self.sources = []
        self.mirror_status = {}
        self.target = 0
        self.conn_restarts = 0
        self.tuning = ""
        self.stalls = 0
        self.writer = None
        self.err_transient = False
        self._refreshed_url = False
        self._abort = None
        self._window = 0
        self._last_tick = time.monotonic()
        self._tune = {}

    # ---------------------------------------------------------------- state
    @property
    def temp_path(self):
        return self.path + TEMP_SUFFIX if self.path else None

    def kids(self):
        if not self.manager:
            return []
        return [k for k in (self.manager.get(c) for c in self.children) if k]

    @property
    def downloaded(self):
        if self.kind == "media":
            return sum(k.downloaded for k in self.kids())
        if self.kind in ("hls", "ytdlp"):
            return self.bytes_done
        with self.lock:
            return sum(s.done for s in self.segments)

    @property
    def expected_total(self):
        if self.kind == "media":
            totals = [k.expected_total for k in self.kids()]
            return sum(totals) if totals and all(totals) else None
        if self.kind == "hls" and self.hls_done and self.status != "Completed":
            return int(self.bytes_done / self.hls_done * self.hls_total)
        return self.total

    @property
    def progress(self):
        if self.status == "Completed":
            return 1.0
        if self.kind == "hls":
            return self.hls_done / self.hls_total if self.hls_total else 0.0
        if self.kind == "media":
            kids = self.kids()
            tot = self.expected_total
            if tot:
                return min(1.0, self.downloaded / tot)
            return sum(k.progress for k in kids) / len(kids) if kids else 0.0
        return min(1.0, self.downloaded / self.total) if self.total else 0.0

    @property
    def eta(self):
        tot = self.expected_total
        if not tot or self.speed < 1024:          # stalled or trickling: no meaningful ETA
            return None
        eta = max(0, tot - self.downloaded) / self.speed
        return eta if eta < 30 * 86400 else None

    @property
    def active_connections(self):
        if self.kind == "media":
            return sum(k.active_connections for k in self.kids())
        with self.lock:
            return sum(1 for w in self.workers if w.alive)

    def worker_stats(self):
        if self.kind == "media":
            return [s for k in self.kids() for s in k.worker_stats()]
        with self.lock:
            return [(w.id, w.source.host, w.speed, w.restarts, w.source.primary)
                    for w in self.workers if w.alive]

    def retarget_extension(self, ext):
        """Change the final file extension (e.g. .ts → .mp4) while the temp file exists."""
        new = unique_path(os.path.splitext(self.path)[0] + ext)
        if self.temp_path and os.path.exists(self.temp_path):
            os.replace(self.temp_path, new + TEMP_SUFFIX)
        self.path, self.filename = new, os.path.basename(new)
        self.category = category_for(self.filename)

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def set_speed_limit(self, kbps):
        self.speed_limit_kbps = max(0, int(kbps or 0))
        self.limiter.set_rate(self.speed_limit_kbps * 1024)

    def to_dict(self):
        data = {k: getattr(self, k) for k in self.PERSIST}
        with self.lock:
            data["segments"] = [[s.start, s.end, s.done] for s in self.segments]
        data["headers"] = {k: v for k, v in self.headers.items()
                           if k.lower() not in _SECRET_HEADERS}
        if data["status"] in ("Verifying", "Extracting"):
            data["status"] = "Completed"
        elif data["status"] in RUNNING_STATES:
            data["status"] = "Queued"
        return data

    @classmethod
    def from_dict(cls, data, manager):
        d = cls(data["url"], manager=manager, headers=data.get("headers"))
        for key in cls.PERSIST:
            if key in data:
                setattr(d, key, data[key])
        d.mirrors = list(d.mirrors or [])
        d.segments = [Segment(*s) for s in data.get("segments", [])]
        d.limiter.set_rate((d.speed_limit_kbps or 0) * 1024)
        if d.status in ("Verifying", "Extracting"):
            d.status = "Completed"
        elif d.status in RUNNING_STATES:
            d.status = "Queued"
        return d

    # ---------------------------------------------------------------- helpers
    def _settings(self):
        return self.manager.settings if self.manager else DEFAULT_SETTINGS

    def _opener(self):
        return self.manager.opener if self.manager else build_opener(DEFAULT_SETTINGS)

    def _headers_for(self, url, extra=None):
        same_host = (urllib.parse.urlsplit(url).hostname
                     == urllib.parse.urlsplit(self.url).hostname)
        h = {"User-Agent": self._settings().get("user_agent") or DEFAULT_UA, "Accept": "*/*",
             "Accept-Encoding": "identity"}
        for k, v in self.headers.items():
            if same_host or k.lower() not in _SECRET_HEADERS:
                h[k] = v
        if self.referrer:
            h["Referer"] = self.referrer
        if self.cookies and same_host:
            h["Cookie"] = self.cookies
        if extra:
            h.update(extra)
        return h

    def _emit(self, kind):
        if self.manager and not self.parent_id:
            self.manager.events.put((kind, self))

    def _save(self):
        if self.manager:
            self.manager.save()

    def _halted(self):
        return self.stop_event.is_set() or self._abort is not None

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.stop_event.clear()
        self.status = "Connecting"
        self.error = ""
        self.retry_at = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"dl-{self.id}")
        self.thread.start()

    def _discard_temp(self):
        if self.temp_path and os.path.exists(self.temp_path):
            try:
                os.remove(self.temp_path)
            except OSError:
                pass
        self.path = None
        with self.lock:
            self.segments = []

    def _prepare(self, info):
        self.final_url = info["final_url"]
        self.total = info["total"]
        self.resumable = info["resumable"] and bool(info["total"])
        self.etag, self.last_modified = info["etag"], info["last_modified"]
        if not self.filename_fixed:
            self.raw_name, self.filename = info["raw_name"], info["name"]
        self.category = category_for(self.filename)
        if self.auto_dir:
            self.save_dir = self.manager.dir_for(self.category) if self.manager else os.getcwd()
        os.makedirs(self.save_dir, exist_ok=True)
        if self.total:
            free = shutil.disk_usage(self.save_dir).free
            if free < self.total + 50 * 1024 * 1024:
                raise BlockedError(f"Not enough disk space: need {self.total / 2**20:.0f} MB, "
                                   f"{free / 2**20:.0f} MB free")
        path = unique_path(os.path.join(self.save_dir, self.filename))
        if not is_inside(path, self.save_dir):
            raise BlockedError("Blocked: the file name tried to escape the download folder")
        self.path, self.filename = path, os.path.basename(path)
        try:
            open(self.temp_path, "wb").close()
            if self.total:
                if self._settings().get("sparse_files", False):
                    make_sparse(self.temp_path)
                with open(self.temp_path, "r+b") as f:
                    f.truncate(self.total)
        except OSError as e:
            if disk_full(e):
                raise
            raise local_file_error(e) from e
        if self.total == 0:
            segs = []
        elif self.resumable and self.sequential:
            piece = max(2 * 1024 * 1024, self.total // 512)
            segs = [Segment(s, min(s + piece, self.total) - 1)
                    for s in range(0, self.total, piece)]
        elif self.resumable:
            n = max(1, min(self._initial_target(), self.total // MIN_SPLIT))
            size = self.total // n
            segs = [Segment(i * size, (i + 1) * size - 1) for i in range(n)]
            segs[-1].end = self.total - 1
        else:
            segs = [Segment(0, self.total - 1 if self.total else None)]
        with self.lock:
            self.segments = segs

    def _cap(self):
        cap = self.connections
        if self.manager and self.sources:
            lim = self.manager.settings.get("host_limits", {}).get(self.sources[0].host)
            if lim:
                cap = min(cap, int(lim))
        return max(1, cap)

    def _initial_target(self):
        return min(self._cap(), AUTO_START) if self.auto_tune else self._cap()

    def _build_sources(self, info):
        sources = [Source(self.url, info["final_url"], validator_of(info), primary=True)]
        self.mirror_status = {}
        if not self.mirrors:
            return sources
        results = {}
        timeout = min(15, self._settings().get("timeout", 30))

        def check(u):
            try:
                mi = self._probe(u)
                if not mi["resumable"]:
                    results[u] = (None, "no resume support")
                elif mi["total"] != self.total:
                    results[u] = (None, "different file size")
                else:
                    results[u] = (Source(u, mi["final_url"], validator_of(mi)), "ok")
            except Exception as e:  # noqa: BLE001
                results[u] = (None, describe_error(e))
        threads = [threading.Thread(target=check, args=(u,), daemon=True) for u in self.mirrors]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout + 5)
        for u in self.mirrors:
            src, msg = results.get(u, (None, "timed out"))
            self.mirror_status[u] = msg
            if src:
                sources.append(src)
        return sources

    def _probe(self, url):
        if urllib.parse.urlsplit(url).scheme in ("ftp", "ftps"):
            return ftp.probe(url, self._settings().get("timeout", 30), ssl_context())
        return probe_url(url, self._headers_for(url), self._opener(),
                         self._settings().get("timeout", 30))

    def _is_ftp(self):
        return urllib.parse.urlsplit(self.url).scheme in ("ftp", "ftps")

    def _run(self):
        self._last_tick = time.monotonic()
        self._abort = None
        self.err_transient = False
        started = self._run_started = time.monotonic()
        try:
            if self.kind == "media":
                if self._run_media():
                    self._finalize()
            elif self.kind == "hls":
                self._run_hls()
            elif self.kind == "ytdlp":
                self._run_ytdlp()
            else:
                self._transfer_file()
            if self._abort:
                raise BlockedError(self._abort)
            if self.status == "Completed":
                self.auto_retries = 0
            elif self.stop_event.is_set():
                self.status = "Paused"
        except BlockedError as e:
            self.status, self.error = "Error", str(e)
            self._emit("blocked")
        except Exception as e:  # noqa: BLE001
            e = as_local(e) or e
            self.status = "Error"
            self.error = ("Disk is full — free some space, then resume" if disk_full(e)
                          else describe_local_error(e) if isinstance(e, LocalFileError)
                          else (str(e) if isinstance(e, (IncompleteError, media.MediaError))
                                else describe_error(e)))
            self.err_transient = is_transient(e)
            s = self._settings()
            code = getattr(e, "code", None)
            if (code in (401, 403, 404, 410) and not self.parent_id and self.media
                    and self.media.get("page_url") and not self._refreshed_url
                    and self._refresh_media_url()):
                self.retry_at = time.time()           # fresh link: retry right away
                self.status = "Error"
            elif (self.err_transient and not self.parent_id and s.get("auto_retry", True)
                    and self.auto_retries < int(s.get("auto_retry_limit", 10))):
                self.auto_retries += 1
                self.retry_at = time.time() + min(300, 10 * 2 ** (self.auto_retries - 1))
                self._emit("retrying")
            else:
                self._emit("failed")
        finally:
            self.elapsed += time.monotonic() - started
            self.speed = 0.0
            with self.lock:
                self.workers = []
            self._save()

    def _refresh_media_url(self):
        """A video link expired: ask yt-dlp for fresh URLs (once per run)."""
        self._refreshed_url = True
        try:
            info = media.resolve(self.media["page_url"], cookies=self.cookies)
            fmt = media.find_format(info, self.media.get("format"))
            if not fmt:
                return False
            self.url = self.final_url = fmt["url"]
            self.headers.update(fmt.get("headers") or {})
            self.url_refreshed = bool(self.segments)
            if self.kind == "hls":
                self.hls = None
            return True
        except Exception:  # noqa: BLE001
            return False

    def _transfer_file(self):
        info = self._probe(self.url)
        if self.stop_event.is_set():
            self.status = "Paused"
            return
        name = self.filename if self.filename_fixed else info["name"]
        if (self._settings().get("block_insecure_executables")
                and urllib.parse.urlsplit(info["final_url"]).scheme in ("http", "ftp")
                and ext_of(name) in security.DANGEROUS_EXTS):
            raise BlockedError("Blocked: program file served over an insecure connection "
                               "(Settings → Security)")
        changed = not self.url_refreshed and (
            (self.etag and info["etag"] and info["etag"] != self.etag)
            or (self.last_modified and info["last_modified"]
                and info["last_modified"] != self.last_modified))
        can_continue = (self.segments and self.path and os.path.exists(self.temp_path)
                        and info["resumable"] and info["total"] == self.total
                        and not changed)
        if self.url_refreshed and self.segments and not can_continue:
            raise BlockedError("The new address points to a different file "
                               f"({info['total']} bytes instead of {self.total})")
        if not can_continue:
            self._discard_temp()
            self._prepare(info)
        else:
            self.final_url = info["final_url"]
            with self.lock:
                for s in self.segments:
                    s.failed = s.active = False
        if self.url_refreshed:
            self.etag, self.last_modified = info["etag"], info["last_modified"]
        self.url_refreshed = False
        self.status = "Downloading"
        self._save()
        if self.resumable:
            self.sources = self._build_sources(info)
            self.writer = FileWriter(self.temp_path)
            try:
                self._run_segmented()
            finally:
                self.writer.close()
                self.writer = None
        else:
            self._run_single()
        if self._abort:
            raise BlockedError(self._abort)
        if self.stop_event.is_set():
            self.status = "Paused"
            return
        with self.lock:
            complete = all(s.remaining() == 0 for s in self.segments)
        if not complete:
            raise IncompleteError(self.error or "Download incomplete")
        self._finalize()

    def _reserve_path(self):
        """For stream/merge jobs: pick the final path once and reserve it."""
        if self.path and os.path.exists(os.path.dirname(self.path)):
            return
        if self.auto_dir or not self.save_dir:
            self.save_dir = self.manager.dir_for(self.category) if self.manager else os.getcwd()
        os.makedirs(self.save_dir, exist_ok=True)
        path = unique_path(os.path.join(self.save_dir, self.filename))
        if not is_inside(path, self.save_dir):
            raise BlockedError("Blocked: the file name tried to escape the download folder")
        self.path, self.filename = path, os.path.basename(path)
        open(self.temp_path, "ab").close()

    def _run_hls(self):
        from . import hls
        self._reserve_path()
        self.status = "Downloading"
        self._save()
        out = hls.run(self, finalize_container=not self.parent_id)
        if out is None:
            if self._abort:
                raise BlockedError(self._abort)
            self.status = "Paused"
            return
        self._finalize()

    def _run_ytdlp(self):
        self._reserve_path()
        self.status = "Downloading"
        tmp = self.temp_path
        if tmp and os.path.exists(tmp) and os.path.getsize(tmp) == 0:
            os.remove(tmp)
        path = media.run_ytdlp(self)
        if path is None:
            self.status = "Paused"
            return
        self.path, self.filename = path, os.path.basename(path)
        self.category = category_for(self.filename)
        self.total = os.path.getsize(path)
        self.bytes_done = self.total
        self._finalize(already_final=True)

    def _run_media(self):
        """Parent job: download the parts in parallel, then merge/convert with ffmpeg."""
        kids = self.kids()
        if len(kids) != len(self.children):
            raise BlockedError("Parts of this download are missing — please add it again")
        self._reserve_path()
        self.status = "Downloading"
        refreshed = False
        while True:
            for k in kids:
                if k.status != "Completed" and not k.is_running():
                    k.stop_event.clear()
                    k._abort = None
                    k.status = "Connecting"
                    k.thread = threading.Thread(target=k._run, daemon=True)
                    k.thread.start()
            last = time.monotonic()
            while any(k.is_running() for k in kids):
                if self.stop_event.is_set():
                    for k in kids:
                        k.stop_event.set()
                now = time.monotonic()
                if now - last >= 0.5:
                    self.speed = sum(k.speed for k in kids)
                    self.history.append(self.speed)
                    last = now
                time.sleep(0.2)
            self.speed = 0.0
            if self.stop_event.is_set():
                self.status = "Paused"
                return False
            failed = [k for k in kids if k.status != "Completed"]
            if not failed:
                break
            err = failed[0].error
            expired = any(c in err for c in ("401", "403", "404", "410", "expired"))
            if expired and not refreshed and (self.media or {}).get("page_url"):
                refreshed = True
                try:
                    info = media.resolve(self.media["page_url"])
                    for k in failed:
                        fmt = media.find_format(info, (k.media or {}).get("format"))
                        if fmt:
                            k.url = k.final_url = fmt["url"]
                            k.url_refreshed = bool(k.segments)
                            k.hls = None if k.kind == "hls" else k.hls
                            k.status = "Paused"
                    continue
                except Exception:  # noqa: BLE001
                    pass
            if failed[0].err_transient:
                raise IncompleteError(err)
            raise BlockedError(err)
        self.status = "Merging"
        ff = media.ffmpeg_path(self._settings())
        op = self.media.get("op", "merge")
        cont = self.media.get("container", "mp4")
        fmt = {"mp4": "mp4", "m4a": "mp4", "mkv": "matroska", "webm": "webm",
               "mp3": "mp3"}.get(cont, "matroska")
        out = self.temp_path
        if op == "mp3":
            args = ["-i", kids[0].path, "-vn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", out]
        else:
            v, a = kids[0].path, kids[1].path
            args = ["-i", v, "-i", a, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]
            if fmt == "mp4":
                args += ["-movflags", "+faststart"]
            args += ["-f", fmt, out]
        media.run_ffmpeg(ff, args)
        for k in kids:
            if self.manager:
                self.manager.remove(k, delete_files=True)
        work = os.path.dirname(kids[0].path)
        shutil.rmtree(work, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(work))          # .pydm-parts, if now empty
        except OSError:
            pass
        self.children = []
        self.total = os.path.getsize(out)
        return True

    def _finalize(self, already_final=False):
        if already_final:
            final = self.path
        else:
            final = self.path
            if os.path.exists(final):
                final = unique_path(final)
            try:
                os.replace(self.temp_path, final)
            except OSError as e:
                raise local_file_error(e) from e
            self.path, self.filename = final, os.path.basename(final)
        if self.total is None:
            self.total = os.path.getsize(final)
        if self.parent_id:                       # part of a merge job: the parent checks it
            self.status, self.error, self.finished = "Completed", "", time.time()
            return
        self.status = "Verifying"
        settings = self._settings()

        published = None
        if (not self.checksum_algo and settings.get("auto_checksum", True) and self.kind == "file"
                and urllib.parse.urlsplit(self.final_url).scheme in ("http", "https")):
            try:
                published = checksums.lookup(self.final_url, self.filename, self._opener(),
                                             {"User-Agent": settings.get("user_agent")
                                              or DEFAULT_UA})
            except Exception:  # noqa: BLE001
                published = None
        algos = ["sha256"] + ([self.checksum_algo] if self.checksum_algo else []) + \
            ([published[0]] if published else [])
        hashes = security.compute_hashes(final, algos)
        self.sha256 = hashes["sha256"]
        findings = security.name_findings(self.raw_name)
        kind, content = security.content_findings(final, self.filename, self.final_url)
        if self.kind in ("hls", "media", "ytdlp"):
            content = [f for f in content if "web page" not in f["text"]]
        findings += content
        if self.checksum_algo:
            if hashes[self.checksum_algo].lower() == self.checksum_value.lower().strip():
                findings.insert(0, security.finding(
                    "info", f"Checksum verified ({self.checksum_algo.upper()} matches)."))
            else:
                findings.insert(0, security.finding(
                    "danger", "Checksum MISMATCH — the file is corrupted or was tampered with. "
                              f"Expected {self.checksum_value[:16]}…, got "
                              f"{hashes[self.checksum_algo][:16]}…"))
        elif published:
            algo, value, src = published
            where = src.rsplit("/", 1)[-1]
            if hashes[algo] == value:
                findings.insert(0, security.finding(
                    "info", f"Checksum verified automatically — {algo.upper()} matches the one "
                            f"published by the website ({where})."))
            else:
                findings.insert(0, security.finding(
                    "danger", f"The website publishes a different {algo.upper()} ({where}) — "
                              "the file is corrupted or was tampered with."))
        if len(self.sources) > 1 and not self.checksum_algo and not published:
            findings.append(security.finding(
                "info", "Downloaded from several mirrors — add a checksum to prove they all "
                        "served identical data."))
        hist = self.manager.history if self.manager else None
        if hist:
            dup = hist.find_hash(self.sha256, exclude_path=final)
            if dup:
                findings.append(security.finding(
                    "info", f"You already have an identical file: {dup['path']}"))
        motw = settings.get("mark_of_the_web") and security.write_mark_of_the_web(
            final, self.final_url if self.kind == "file" else
            ((self.media or {}).get("page_url") or self.url), self.referrer)
        av = None
        if settings.get("antivirus_scan"):
            status, detail = security.antivirus_scan(final)
            av = {"status": status, "detail": detail}
            if status in ("threat", "removed"):
                findings.insert(0, security.finding("danger", detail))
            elif status == "clean":
                findings.append(security.finding("info", detail))
        vt = None
        key = (settings.get("virustotal_api_key") or "").strip()
        if key:
            vt = security.virustotal_lookup(self.sha256, key, self._opener())
            if vt.get("status") == "found":
                if vt["malicious"] or vt["suspicious"]:
                    findings.insert(0, security.finding(
                        "danger", f"VirusTotal: {vt['malicious']} of {vt['total']} engines flag "
                                  f"this file as malicious ({vt['suspicious']} suspicious)."))
                else:
                    findings.append(security.finding(
                        "info", f"VirusTotal: 0 of {vt['total']} engines flag this file."))
            elif vt.get("status") == "unknown":
                findings.append(security.finding("info", "VirusTotal has never seen this file."))
        level = security.overall(findings)

        if level != "danger" and self.extract and postprocess.is_archive(self.filename) \
                and os.path.exists(final):
            self.status = "Extracting"
            try:
                dest, count = postprocess.extract(
                    final, self.final_url if settings.get("mark_of_the_web") else None,
                    self.referrer)
                self.extracted_to = dest
                findings.append(security.finding("info", f"Extracted {count} files safely."))
                if settings.get("delete_archive_after_extract"):
                    os.remove(final)
            except Exception as e:  # noqa: BLE001
                findings.append(security.finding("caution", f"Not extracted: {e}"))
            level = security.overall(findings)

        self.security = {"level": level, "findings": findings, "kind": kind, "av": av,
                         "vt": vt, "motw": bool(motw),
                         "checksum": published[2] if published else None}
        cmd = (settings.get("post_command") or "").strip()
        if cmd and level != "danger" and os.path.exists(final):
            try:
                postprocess.run_command(cmd, final, self.url)
            except Exception as e:  # noqa: BLE001
                self.security["findings"].append(
                    security.finding("caution", f"Post-download command failed: {e}"))
        self.status, self.error, self.finished = "Completed", "", time.time()
        if hist and settings.get("history_enabled", True):
            try:
                hist.record(self, max(self.elapsed + time.monotonic()
                                      - getattr(self, "_run_started", time.monotonic()), 0.001))
            except Exception:  # noqa: BLE001
                pass
        self._emit("threat" if level == "danger" else "completed")

    # ---------------------------------------------------------------- segmented transfer
    def _tick_speed(self):
        now = time.monotonic()
        dt = now - self._last_tick
        if dt < 0.5:
            return
        with self.lock:
            w_total, self._window = self._window, 0
            for w in self.workers:
                inst = w.window / dt
                w.window = 0
                w.speed = inst if w.speed == 0 else w.speed * 0.5 + inst * 0.5
            for s in self.sources:
                inst = (s.bytes - s._last) / dt
                s._last = s.bytes
                s.speed = inst if s.speed == 0 else s.speed * 0.6 + inst * 0.4
        inst = w_total / dt
        self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
        self.history.append(self.speed)
        self._last_tick = now

    def _has_work(self):
        for s in self.segments:
            if not s.active and not s.failed and s.remaining() > 0:
                return True
        return not self.sequential and self._best_split() is not None

    def _best_split(self):
        """Pick the active piece that will take longest to finish (by its connection's speed),
        and where to cut it. Caller holds self.lock. Returns (segment, cut) or None."""
        owners = {id(w.seg): w for w in self.workers if w.alive and w.seg is not None}
        speeds = [w.speed for w in owners.values() if w.speed > 0]
        med = statistics.median(speeds) if speeds else 0
        best, best_eta = None, 0
        for seg in self.segments:
            if not seg.active:
                continue
            rem = seg.remaining()
            owner = owners.get(id(seg))
            own = owner.speed if owner else 0
            slow = med > 0 and own < med * 0.5
            if rem < 2 * (ENDGAME_SPLIT if slow else MIN_SPLIT):
                continue
            eta = rem / max(own, 32 * 1024)
            if eta > best_eta:
                best, best_eta = (seg, own), eta
        if best is None:
            return None
        seg, own = best
        rem = seg.remaining()
        # the owner keeps the share it can finish in the same time a typical connection would
        keep = rem // 2 if not med or not own else int(rem * own / (own + med))
        keep = max(min(keep, rem - ENDGAME_SPLIT), 0)
        return seg, seg.pos + keep

    def _split_largest(self):
        pick = self._best_split()
        if pick is None:
            return None
        big, cut = pick
        new = Segment(cut, big.end)
        big.end = cut - 1
        self.segments.append(new)
        self.segments.sort(key=lambda s: s.start)
        return new

    def _claim(self, w):
        with self.lock:
            seg = next((s for s in self.segments
                        if not s.active and not s.failed and s.remaining() > 0), None)
            if seg is None and not self.sequential:
                seg = self._split_largest()
            if seg is not None:
                seg.active = True
                w.seg = seg
            return seg

    def _pick_source(self):
        ok = [s for s in self.sources if not s.disabled]
        if not ok:
            return None
        return min(ok, key=lambda s: (s.active + 1) / (s.per_conn() + 1))

    def _switch_source(self, w):
        new = self._pick_source()
        if new is None or new is w.source:
            return
        with self.lock:
            w.source.active -= 1
            new.active += 1
            w.source = new

    def _run_segmented(self):
        with self.lock:
            self.workers = []
        self.target = self._initial_target()
        self.conn_restarts = 0
        self._tune = {"t": time.monotonic(), "base": None, "prev": None, "done": False,
                      "done_at": 0}
        self.tuning = "tuning" if self.auto_tune else "fixed"
        last_save = last_slow = time.monotonic()
        while not self._halted():
            with self.lock:
                self.workers = [w for w in self.workers if w.alive]
                alive = len(self.workers)
                work = self._has_work()
            if not alive and not work:
                break
            slots = self.target - alive
            if self.manager:
                slots = min(slots, self.manager.connection_budget())
            for _ in range(max(0, slots) if work else 0):
                src = self._pick_source()
                if src is None:
                    self._abort = "All download sources failed"
                    break
                w = Worker(src)
                with self.lock:
                    self.workers.append(w)
                    src.active += 1
                threading.Thread(target=self._worker, args=(w,), daemon=True).start()
            self._tick_speed()
            self._autotune()
            now = time.monotonic()
            if now - last_slow >= 1.0:
                self._replace_slow(now - last_slow)
                self._watchdog(now)
                last_slow = now
            if now - last_save > 3:
                self._save()
                last_save = now
            time.sleep(0.15)
        while True:
            with self.lock:
                if not any(w.alive for w in self.workers):
                    break
            time.sleep(0.05)

    def _limited(self):
        return self.limiter.rate > 0 or (self.manager and self.manager.limiter.rate > 0)

    def _autotune(self):
        """Double the connection count while throughput keeps scaling; settle when it stops."""
        t = self._tune
        if not self.auto_tune or self._limited():
            return
        now = time.monotonic()
        if t["done"]:
            if now - t["done_at"] < 30:
                return
            t.update(done=False, base=None, prev=None, t=now, bytes=self.downloaded)
            self.tuning = "tuning"
        if "bytes" not in t:
            t["bytes"] = self.downloaded
        if now - t["t"] < TUNE_INTERVAL:
            return
        done_now = self.downloaded
        cur = (done_now - t["bytes"]) / (now - t["t"])
        t["t"], t["bytes"] = now, done_now
        if t["base"] is not None:
            gain = cur / t["base"] if t["base"] else 2
            if gain < 1.05:                    # more connections didn't help: go back
                self.target = t["prev"]
                self._retire_extra()
                t.update(done=True, done_at=now)
                self.tuning = "optimal"
                return
            if gain < 1.25:                    # small gain: keep it, stop growing
                t.update(done=True, done_at=now)
                self.tuning = "optimal"
                return
        with self.lock:
            alive = sum(1 for w in self.workers if w.alive)
            remaining = (self.total or 0) - sum(s.done for s in self.segments)
        cap = self._cap()
        if self.target >= cap:
            t.update(done=True, done_at=now)
            self.tuning = "max"
            return
        if alive < self.target or remaining < self.target * 4 * MIN_SPLIT or cur <= 0:
            t["base"] = None
            return
        t["prev"], t["base"] = self.target, cur
        self.target = min(cap, self.target * 2)

    def _retire_extra(self):
        with self.lock:
            alive = sorted((w for w in self.workers if w.alive and not w.retire),
                           key=lambda w: w.speed)
            for w in alive[:max(0, len(alive) - self.target)]:
                w.retire = True

    def _throttled(self, w):
        """Server refused more connections (429/503): back off and remember the limit."""
        with self.lock:
            alive = sum(1 for x in self.workers if x.alive)
        self.target = max(1, min(self.target, alive - 1))
        self._tune.update(done=True, done_at=time.monotonic())
        self.tuning = "server limit"
        if self.manager and w.source.primary and self.target < self._cap():
            limits = dict(self.manager.settings.get("host_limits", {}))
            limits[w.source.host] = self.target
            self.manager.update_settings({"host_limits": limits})

    def _watchdog(self, now):
        """Every connection silent: reconnect them all instead of waiting out the socket timeout.

        Some servers and ISPs (or a Wi-Fi drop) leave TCP connections open but send nothing.
        After STALL_SECONDS with no bytes at all, cut every connection and redial. If that keeps
        happening, the server is probably punishing many connections: halve them."""
        wd = self.__dict__.setdefault("_wd", {"bytes": self.downloaded, "t": now, "kicks": 0})
        if self.downloaded != wd["bytes"]:
            wd.update(bytes=self.downloaded, t=now)
            if now - wd.get("kick_t", 0) > 60:
                wd["kicks"] = 0
            return
        if now - wd["t"] < STALL_SECONDS:
            return
        with self.lock:
            ws = [w for w in self.workers if w.alive and w.seg and not w.retire]
        if not ws:
            return
        wd["kicks"] += 1
        wd.update(t=now, kick_t=now)
        self.stalls += 1
        if wd["kicks"] >= 2 and self.target > 1:
            self.target = max(1, self.target // 2)
            self._tune.update(done=True, done_at=now)
            self.tuning = "stalled"
        for w in ws:
            w.restart = True
            if w.conn is not None and hasattr(w.conn, "abort"):
                w.conn.abort()

    def _replace_slow(self, interval):
        if not self._settings().get("replace_slow_connections", True):
            return
        now = time.monotonic()
        with self.lock:
            ws = [w for w in self.workers
                  if w.alive and w.seg and not w.restart and now - w.started > 3]
            if len(ws) < 2:
                return
            # Reference speed: the median, or, when most connections are dead (median ~0),
            # the best one. A silent connection next to a working one is never "normal".
            ref = max(statistics.median(w.speed for w in ws), max(w.speed for w in ws) * 0.5)
            if ref < 64 * 1024:
                return                              # everything is slow: the watchdog's job
            cut = []
            for w in ws:
                dead = w.speed < 16 * 1024
                if (w.speed < ref * 0.3 or dead) and w.seg.remaining() > 4 * CHUNK_SIZE:
                    w.slow_for += interval
                    if w.slow_for >= (2.0 if dead else 2.5) and (w.restarts < 4 or dead):
                        w.restart = True
                        w.restarts += 1
                        w.slow_for = 0
                        self.conn_restarts += 1
                        cut.append(w)
                else:
                    w.slow_for = 0
        for w in cut:                               # a silent read never returns by itself
            if w.conn is not None and hasattr(w.conn, "abort"):
                w.conn.abort()

    def _consume(self, n):
        if self.manager:
            self.manager.limiter.consume(n, self.stop_event)
        self.limiter.consume(n, self.stop_event)

    def _worker(self, w):
        conn = ftp.FtpConn(self) if self._is_ftp() else HttpConn(self)
        w.conn = conn
        seg = None
        try:
            while not self._halted() and not w.retire:
                seg = self._claim(w)
                if seg is None:
                    return
                attempts = 0
                while not self._halted() and not w.retire:
                    try:
                        if self._fetch(w, conn, seg):
                            break
                        if w.restart:                 # slow connection: redial, maybe elsewhere
                            conn.close()
                            w.restart = False
                            self._switch_source(w)
                            w.started, w.speed = time.monotonic(), 0.0
                    except Exception as e:  # noqa: BLE001
                        conn.close()
                        if self._halted():
                            return
                        if w.restart and not w.retire:        # watchdog cut a silent connection
                            w.restart = False
                            self._switch_source(w)
                            w.started, w.speed = time.monotonic(), 0.0
                            continue
                        action = self._on_error(w, seg, e, attempts)
                        if action != "retry":
                            return
                        attempts += 1
                with self.lock:
                    seg.active = False
                    w.seg = None
                seg = None
        finally:
            conn.close()
            with self.lock:
                if seg is not None:
                    seg.active = False
                w.alive = False
                w.seg = None
                w.source.active -= 1

    def _on_error(self, w, seg, e, attempts):
        code = getattr(e, "code", None)
        if disk_full(e):
            self._abort = "Disk is full — free some space, then resume"
            return "abort"
        if isinstance(e, BlockedError):
            self._abort = str(e)
            return "abort"
        if as_local(e):                             # our disk, not the network: don't redial
            self._abort = describe_local_error(as_local(e))
            return "abort"
        if code in (429, 503):
            self._throttled(w)
            self.stop_event.wait(min(getattr(e, "retry_after", None) or 3, 30))
            return "drop"
        src = w.source
        if code and 400 <= code < 500 and code not in (408,):
            if src.final_url != src.url:              # cached redirect target expired
                src.final_url = src.url
                return "retry"
            others = [s for s in self.sources if s is not src and not s.disabled]
            if others:
                src.disabled = True
                self._switch_source(w)
                return "retry"
            self._abort = describe_error(urllib.error.HTTPError(src.url, code, "", {}, None))
            return "abort"
        if attempts + 1 > max(1, int(self._settings().get("max_retries", 8))):
            seg.failed = True
            self.error = describe_error(e)
            return "drop"
        self.stop_event.wait(min(2 ** attempts * 0.5, 15))
        return "retry"

    def _fetch(self, w, conn, seg):
        """Download (part of) seg over conn. True when seg is complete."""
        with self.lock:
            start, end = seg.pos, seg.end
        if start > end:
            return True
        src = w.source
        r, used = conn.get_range(src.final_url, start, end, src.validator)
        try:
            if r.status == 416 or (r.status == 200 and src.validator):
                raise FileChanged()
            if r.status != 206:
                if r.status >= 400:
                    raise HTTPStatusError(r.status, r.reason, r.getheader("Retry-After"))
                raise BlockedError(f"Server stopped accepting range requests (HTTP {r.status})")
            m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", r.getheader("Content-Range") or "")
            if not m or int(m.group(1)) != start:
                raise IOError("Server returned the wrong byte range")
            if m.group(3) != "*" and int(m.group(3)) != self.total:
                raise FileChanged()
            src.final_url = used
            mv, pos, interrupted = w.mv, start, False
            while True:
                if self._halted() or w.retire or w.restart:
                    interrupted = True
                    break
                with self.lock:
                    want = seg.end - pos + 1
                if want <= 0:
                    break
                n = r.readinto(mv[:min(CHUNK_SIZE, want)])
                if not n:
                    raise IOError("Connection closed early")
                self._consume(n)
                with self.lock:
                    n = min(n, seg.end - pos + 1)   # seg may have been split meanwhile
                if n <= 0:
                    break
                self.writer.write(pos, mv[:n])
                pos += n
                with self.lock:
                    seg.done += n
                    self._window += n
                    w.window += n
                    src.bytes += n
            if interrupted or r.length:                # unread bytes: connection not reusable
                conn.close()
            with self.lock:
                return seg.remaining() == 0
        except BaseException:
            conn.close()
            raise

    def _run_single(self):
        if not self.segments:
            return
        seg = self.segments[0]
        with self.lock:
            seg.done = 0
        req = urllib.request.Request(self.final_url, headers=self._headers_for(self.final_url))
        buf = bytearray(CHUNK_SIZE)
        mv = memoryview(buf)
        with self._opener().open(req, timeout=self._settings().get("timeout", 30)) as r, \
                open(self.temp_path, "wb", buffering=0) as f:
            while not self.stop_event.is_set():
                n = r.readinto(mv)
                if not n:
                    break
                self._consume(n)
                f.write(mv[:n])
                with self.lock:
                    seg.done += n
                    self._window += n
                self._tick_speed()
        if self.stop_event.is_set():
            return
        if seg.end is None:
            with self.lock:
                seg.end = seg.done - 1
            self.total = seg.done
        elif seg.remaining() > 0:
            raise IncompleteError("Connection closed early")


# --------------------------------------------------------------------------- manager
class Manager:
    def __init__(self, persist=True):
        self.persist = persist
        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.downloads = []
        self.lock = threading.RLock()
        self._save_lock = threading.Lock()
        self.limiter = RateLimiter()
        self.events = queue.Queue()
        self._sched_fired = {}
        self._was_busy = False
        self.history = None
        if persist:
            os.makedirs(APP_DIR, exist_ok=True)
            if os.name == "posix":
                os.chmod(APP_DIR, 0o700)
            self._load()
            try:
                from .history import History
                self.history = History()
            except Exception:  # noqa: BLE001
                self.history = None
        self.governor = Governor(self.settings, self.total_speed)
        self._applied_limit = None
        self.bandwidth_reason = ""
        self.apply_bandwidth()
        self.opener = build_opener(self.settings)

    def _load(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.settings.update({k: v for k, v in json.load(f).items()
                                      if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                for item in json.load(f):
                    self.downloads.append(Download.from_dict(item, self))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _write_private(self, path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def save(self):
        if not self.persist:
            return
        with self.lock:
            data = [d.to_dict() for d in self.downloads]
        with self._save_lock:
            self._write_private(STATE_FILE, data)

    def total_speed(self):
        return sum(d.speed for d in self.snapshot() if d.is_running() and not d.parent_id)

    def apply_bandwidth(self):
        lim = self.governor.effective_limit()
        self.bandwidth_reason = self.governor.reason
        if lim != self._applied_limit:
            self._applied_limit = lim
            self.limiter.set_rate(lim)

    def update_settings(self, new):
        self.settings.update(new)
        self.apply_bandwidth()
        self.opener = build_opener(self.settings)
        if self.persist:
            with self._save_lock:
                self._write_private(SETTINGS_FILE, self.settings)

    def default_connections(self):
        s = self.settings
        return (int(s["max_connections"]) if s["auto_connections"] else int(s["connections"]),
                bool(s["auto_connections"]))

    def connection_budget(self):
        with self.lock:
            used = sum(d.active_connections for d in self.downloads if d.is_running())
        return max(0, int(self.settings["max_total_connections"]) - used)

    def dir_for(self, category):
        base = self.settings["download_dir"]
        return os.path.join(base, category) if self.settings["use_categories"] else base

    def get(self, did):
        with self.lock:
            return next((d for d in self.downloads if d.id == did), None)

    def find_url(self, url):
        url = url.strip()
        with self.lock:
            return next((d for d in self.downloads if not d.parent_id and
                         (d.url == url or (d.media or {}).get("page_url") == url)), None)

    def snapshot(self):
        with self.lock:
            return list(self.downloads)

    def visible(self):
        """Top-level downloads (parts of a merge job are hidden)."""
        with self.lock:
            return [d for d in self.downloads if not d.parent_id]

    def add(self, url, save_dir=None, filename=None, referrer=None, cookies=None, start=True,
            source="manual", checksum=None, speed_limit_kbps=0, headers=None, connections=None,
            auto_tune=None, mirrors=None, sequential=False, extract=None, kind="file",
            media=None, parent_id=None):
        conns, auto = self.default_connections()
        if kind in ("hls", "ytdlp") and connections is None:
            conns = min(conns, 16)
        d = Download(url, save_dir, filename, referrer, cookies,
                     int(connections or conns), self, source, checksum, speed_limit_kbps,
                     headers, auto if auto_tune is None else auto_tune, mirrors, sequential,
                     self.settings["extract_archives"] if extract is None else extract,
                     kind, parent_id, media)
        d.status = "Queued" if start and not parent_id else "Paused"
        with self.lock:
            self.downloads.append(d)
        self.save()
        return d

    def add_media(self, info, option, save_dir=None, start=True, referrer=None, cookies=None,
                  source="manual"):
        """Add a video download for a quality option from media.choices()."""
        cont = option["container"]
        filename = media.title_filename(info["title"], cont)
        base = {"page_url": info.get("page_url"), "title": info["title"],
                "label": option["label"], "height": option.get("height"), "container": cont}
        parts = [f for f in (option.get("video"), option.get("audio")) if f]
        engines = {media.engine_for(f) for f in parts}
        ffmpeg = media.ffmpeg_path(self.settings)
        common = dict(save_dir=save_dir, referrer=referrer, cookies=cookies, start=start,
                      source=source)
        if "ytdlp" in engines or (option["kind"] in ("merge", "mp3") and not ffmpeg):
            if not info.get("page_url"):
                raise media.MediaError("This stream needs yt-dlp")
            fmt = "+".join(f["id"] for f in parts)
            return self.add(info["page_url"], filename=filename, kind="ytdlp",
                            media=dict(base, format=fmt,
                                       op="mp3" if option["kind"] == "mp3" else None), **common)
        if option["kind"] in ("single", "audio"):
            f = parts[0]
            kind = media.engine_for(f)
            return self.add(f["url"], filename=filename, kind=kind, headers=f.get("headers"),
                            media=dict(base, format=f["id"]), **common)
        op = "mp3" if option["kind"] == "mp3" else "merge"
        parent = self.add(info.get("page_url") or parts[0]["url"], filename=filename,
                          kind="media", media=dict(base, op=op), start=False,
                          save_dir=save_dir, referrer=referrer, cookies=cookies, source=source)
        work_dir = os.path.join(parent.save_dir or self.dir_for(category_for(filename)),
                                ".pydm-parts", parent.id)
        kids = []
        for role, f in zip(("video", "audio") if op == "merge" else ("audio",), parts):
            kind = media.engine_for(f)
            ext = f["ext"] if kind == "file" else "ts"
            k = self.add(f["url"], save_dir=work_dir, filename=f"{role}.{ext}", kind=kind,
                         headers=f.get("headers"), referrer=referrer, cookies=cookies,
                         start=False, parent_id=parent.id,
                         media={"page_url": info.get("page_url"), "format": f["id"],
                                "height": option.get("height")}, source=source)
            kids.append(k.id)
        parent.children = kids
        parent.status = "Queued" if start else "Paused"
        self.save()
        return parent

    def inspect_stream(self, url, headers=None, referrer=None, cookies=None):
        """Read an HLS master playlist: returns list of quality options for add_stream."""
        from . import hls
        probe = Download(url, manager=self, headers=headers, referrer=referrer, cookies=cookies)
        text, final = hls.fetch_text(probe, url)
        pl = hls.parse_playlist(text, final)
        if pl["type"] == "media":
            return {"type": "media", "url": final, "options": [], "drm": pl["drm"],
                    "duration": pl["duration"]}
        opts = []
        for v in pl["variants"]:
            audio = None
            if v.get("audio"):
                group = [a for a in pl["audio"] if a["group"] == v["audio"]]
                pick = next((a for a in group if a["default"]), group[0] if group else None)
                audio = pick["url"] if pick else None
            label = f"{v['height']}p" if v["height"] else f"{v['bandwidth'] // 1000} kbps"
            opts.append({"label": label, "url": v["url"], "audio_url": audio,
                         "height": v["height"], "bandwidth": v["bandwidth"]})
        return {"type": "master", "url": final, "options": opts}

    def add_stream(self, url, title="video", height=None, start=True, save_dir=None,
                   referrer=None, cookies=None, headers=None, source="manual", page_url=None):
        """Add an HLS stream; separate audio renditions become a merged video+audio job."""
        filename = media.title_filename(title, "mp4")
        info = None
        try:
            info = self.inspect_stream(url, headers, referrer, cookies)
        except Exception:  # noqa: BLE001
            pass
        base = {"title": title, "height": height, "page_url": page_url, "container": "mp4"}
        if info and info["type"] == "master" and info["options"]:
            opts = info["options"]
            pick = opts[0]
            if height:
                fits = [o for o in opts if (o["height"] or 0) <= height]
                pick = fits[0] if fits else opts[-1]
            if pick["audio_url"] and media.ffmpeg_path(self.settings):
                parent = self.add(url, filename=filename, kind="media",
                                  media=dict(base, op="merge"), start=False, save_dir=save_dir,
                                  referrer=referrer, cookies=cookies, source=source)
                work = os.path.join(parent.save_dir or self.dir_for("Video"), ".pydm-parts",
                                    parent.id)
                kids = []
                for role, u in (("video", pick["url"]), ("audio", pick["audio_url"])):
                    k = self.add(u, save_dir=work, filename=f"{role}.ts", kind="hls",
                                 headers=headers, referrer=referrer, cookies=cookies,
                                 start=False, parent_id=parent.id, media={"height": None},
                                 source=source)
                    kids.append(k.id)
                parent.children = kids
                parent.status = "Queued" if start else "Paused"
                self.save()
                return parent
            url = pick["url"]
        return self.add(url, filename=filename, kind="hls", headers=headers, referrer=referrer,
                        cookies=cookies, start=start, save_dir=save_dir, source=source,
                        media=dict(base, height=height))

    def pause(self, d):
        d.retry_at = None
        if d.is_running():
            if d.status not in ("Verifying", "Extracting"):
                d.stop_event.set()
        elif d.status in ("Queued", "Error"):
            d.status = "Paused"
        self.save()

    def resume(self, d):
        if not d.is_running() and d.status in ("Paused", "Error"):
            d.status = "Queued"
            d.retry_at = None
            d.auto_retries = 0
            self.save()

    def pause_all(self):
        for d in self.visible():
            if d.status not in ("Completed",) and not (d.status == "Error" and not d.retry_at):
                self.pause(d)

    def resume_all(self):
        for d in self.visible():
            if d.status == "Paused":
                self.resume(d)

    def refresh_url(self, d, url):
        if d.is_running():
            raise ValueError("Pause the download first")
        d.url = d.final_url = url.strip()
        d.url_refreshed = bool(d.segments)
        d.cookies = None
        d.retry_at = None
        if d.status == "Error":
            d.status = "Paused"
        self.save()

    def move(self, d, delta):
        with self.lock:
            i = self.downloads.index(d)
            j = max(0, min(len(self.downloads) - 1, i + delta))
            self.downloads.insert(j, self.downloads.pop(i))
        self.save()

    def remove(self, d, delete_files=False):
        d.stop_event.set()
        kids = d.kids() if d.kind == "media" else []
        with self.lock:
            if d in self.downloads:
                self.downloads.remove(d)
        for k in kids:
            self.remove(k, delete_files=True)
        self.save()

        def cleanup():
            if d.thread:
                d.thread.join(timeout=10)
            if delete_files:
                for p in (d.temp_path, d.path if d.status == "Completed" else None):
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                if d.path and os.path.isdir(d.path + ".pydm-hls"):
                    shutil.rmtree(d.path + ".pydm-hls", ignore_errors=True)
                if kids and kids[0].save_dir:
                    shutil.rmtree(kids[0].save_dir, ignore_errors=True)
        threading.Thread(target=cleanup, daemon=True).start()

    def clear_completed(self):
        with self.lock:
            self.downloads = [d for d in self.downloads
                              if d.status != "Completed" or d.parent_id]
        self.save()

    def in_schedule_window(self):
        s = self.settings
        now = time.strftime("%H:%M")
        a, b = s["scheduler_start"], s["scheduler_stop"]
        return a <= now < b if a <= b else (now >= a or now < b)

    def _scheduler(self):
        s = self.settings
        if not s["scheduler_enabled"]:
            return
        now, today = time.strftime("%H:%M"), time.strftime("%Y-%m-%d")
        if now == s["scheduler_start"] and self._sched_fired.get("start") != today:
            self._sched_fired["start"] = today
            self.resume_all()
            self.events.put(("schedule_start", None))
        if now == s["scheduler_stop"] and self._sched_fired.get("stop") != today:
            self._sched_fired["stop"] = today
            for d in self.snapshot():
                if d.is_running():
                    self.pause(d)
                    d.status = "Queued"
            self.events.put(("schedule_stop", None))

    def schedule(self):
        self._scheduler()
        self.apply_bandwidth()
        now = time.time()
        hold = (self.settings["scheduler_enabled"] and self.settings["scheduler_window_only"]
                and not self.in_schedule_window())
        with self.lock:
            for d in self.downloads:
                if d.parent_id:
                    continue
                if d.status == "Error" and d.retry_at and now >= d.retry_at and \
                        not d.is_running():
                    d.retry_at = None
                    d.status = "Queued"
            running = sum(1 for d in self.downloads if d.is_running() and not d.parent_id)
            if not hold:
                for d in self.downloads:
                    if running >= int(self.settings["max_active"]):
                        break
                    if d.status == "Queued" and not d.is_running() and not d.parent_id:
                        d.start()
                        running += 1
            queued = any(d.status == "Queued" or (d.status == "Error" and d.retry_at)
                         for d in self.downloads)
        busy = running > 0 or (queued and not hold)
        if self._was_busy and not busy:
            self.events.put(("queue_done", None))
        self._was_busy = busy

    def any_running(self):
        return any(d.is_running() for d in self.visible())

    def shutdown(self):
        running = [d for d in self.visible() if d.is_running()]
        for d in running:
            d.stop_event.set()
        for d in running:
            d.thread.join(timeout=5)
            if d.status in ("Connecting", "Downloading", "Paused"):
                d.status = "Queued"
        self.save()
