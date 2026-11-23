"""Local endpoint the browser extension talks to.

Hardening:
  * binds to 127.0.0.1 only
  * Host header must be 127.0.0.1/localhost (defeats DNS-rebinding attacks)
  * requests must carry an extension Origin; ordinary web pages are rejected
  * each extension must be approved once in the app (pairing) before it can add downloads
  * JSON only, body size capped, per-origin rate limit
"""
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .util import APP_NAME, VERSION, is_http_url

EXT_SCHEMES = ("chrome-extension://", "moz-extension://", "safari-web-extension://")
RATE_LIMIT = 30          # requests per origin per minute


class Bridge:
    def __init__(self, port, manager):
        self.port = port
        self.manager = manager
        self.inbox = queue.Queue()          # download requests for the UI
        self.pair_requests = queue.Queue()  # origins awaiting approval
        self._pending = {}                  # origin -> [Event, allowed]
        self._denied = set()
        self._hits = {}
        self._lock = threading.Lock()
        self.server = None
        self.last_contact = 0.0

    # ------------------------------------------------------------ trust
    def is_trusted(self, origin):
        return origin in self.manager.settings.get("trusted_origins", [])

    def resolve_pairing(self, origin, allowed):
        with self._lock:
            entry = self._pending.pop(origin, None)
            if not allowed:
                self._denied.add(origin)
        if allowed and not self.is_trusted(origin):
            trusted = list(self.manager.settings.get("trusted_origins", [])) + [origin]
            self.manager.update_settings({"trusted_origins": trusted})
        if entry:
            entry[1] = allowed
            entry[0].set()

    def _await_pairing(self, origin, timeout=90):
        with self._lock:
            if origin in self._denied:
                return False
            entry = self._pending.get(origin)
            if entry is None:
                entry = self._pending[origin] = [threading.Event(), False]
                self.pair_requests.put(origin)
        entry[0].wait(timeout)
        return entry[1]

    def _rate_ok(self, origin):
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(origin, []) if now - t < 60]
            ok = len(hits) < RATE_LIMIT
            if ok:
                hits.append(now)
            self._hits[origin] = hits
            return ok

    # ------------------------------------------------------------ server
    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PyDM"
            sys_version = ""

            def _host_ok(self):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                return host in ("127.0.0.1", "localhost", "::1")

            def _origin(self):
                o = self.headers.get("Origin") or ""
                return o if o.startswith(EXT_SCHEMES) else None

            def _reply(self, code, body=None):
                payload = json.dumps(body or {}).encode()
                self.send_response(code)
                origin = self._origin()
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type")
                    self.send_header("Vary", "Origin")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def _guard(self):
                if not self._host_ok():
                    self._reply(421, {"ok": False, "error": "bad host"})
                    return None
                origin = self._origin()
                if not origin:
                    self._reply(403, {"ok": False, "error": "forbidden"})
                    return None
                if not bridge._rate_ok(origin):
                    self._reply(429, {"ok": False, "error": "rate limited"})
                    return None
                return origin

            def do_OPTIONS(self):
                if self._host_ok() and self._origin():
                    self._reply(204)
                else:
                    self._reply(403)

            def do_GET(self):
                origin = self._guard()
                if not origin:
                    return
                if self.path == "/ping":
                    bridge.last_contact = time.time()
                    self._reply(200, {"app": APP_NAME, "version": VERSION,
                                      "paired": bridge.is_trusted(origin)})
                else:
                    self._reply(404, {"ok": False})

            def do_POST(self):
                origin = self._guard()
                if not origin:
                    return
                if self.path == "/ping":
                    bridge.last_contact = time.time()
                    return self._reply(200, {"app": APP_NAME, "version": VERSION,
                                             "paired": bridge.is_trusted(origin)})
                if self.path not in ("/add", "/batch", "/media"):
                    return self._reply(404, {"ok": False})
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    return self._reply(415, {"ok": False})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > 2 * 1024 * 1024:
                    return self._reply(413, {"ok": False})
                try:
                    data = json.loads(self.rfile.read(length))
                    assert isinstance(data, dict)
                except (ValueError, AssertionError):
                    return self._reply(400, {"ok": False})
                def s(key, limit, src=data):
                    v = src.get(key)
                    return str(v)[:limit] if v else None
                ref = s("referrer", 8192)
                ref = ref if is_http_url(ref or "") else None
                if self.path == "/add":
                    url = str(data.get("url", ""))[:8192]
                    if not is_http_url(url):
                        return self._reply(400, {"ok": False, "error": "invalid url"})
                    item = {"type": "add", "url": url, "referrer": ref,
                            "cookies": s("cookies", 16384), "filename": s("filename", 255),
                            "size": data.get("size") if isinstance(data.get("size"), int)
                            else None}
                elif self.path == "/batch":
                    links = data.get("links")
                    if not isinstance(links, list) or not links:
                        return self._reply(400, {"ok": False, "error": "no links"})
                    clean = []
                    for ln in links[:5000]:
                        u = str(ln.get("url") if isinstance(ln, dict) else ln)[:8192]
                        if is_http_url(u):
                            clean.append(u)
                    if not clean:
                        return self._reply(400, {"ok": False, "error": "no valid links"})
                    item = {"type": "batch", "links": clean, "referrer": ref}
                else:
                    page = str(data.get("page_url", ""))[:8192]
                    if not is_http_url(page):
                        return self._reply(400, {"ok": False, "error": "invalid page"})
                    cands = []
                    for c in (data.get("candidates") or [])[:50]:
                        if isinstance(c, dict) and is_http_url(str(c.get("url", ""))):
                            cands.append({"url": str(c["url"])[:8192],
                                          "type": str(c.get("type", ""))[:40],
                                          "size": c.get("size") if isinstance(c.get("size"), int)
                                          else None})
                    item = {"type": "media", "page_url": page, "title": s("title", 300),
                            "candidates": cands, "cookies": s("cookies", 16384),
                            "referrer": ref or page}
                if not bridge.is_trusted(origin) and not bridge._await_pairing(origin):
                    return self._reply(403, {"ok": False, "error": "not approved in PyDM"})
                bridge.last_contact = time.time()
                item["origin"] = origin
                bridge.inbox.put(item)
                self._reply(200, {"ok": True})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
