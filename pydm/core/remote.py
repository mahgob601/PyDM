"""Remote control: a small web app for your phone (same Wi-Fi) to add and manage downloads.

Security
  * every API call needs the secret access token (256-bit), compared in constant time
  * the token travels in the URL *fragment* (#t=…) so it never reaches server logs/Referer,
    then lives in the phone's local storage and is sent as an Authorization header
  * repeated wrong tokens from an address are locked out for 15 minutes
  * strict Content-Security-Policy, no CORS, clickjacking protection, body size limits
  * only http(s)/ftp(s) URLs can be added; downloads always go to the configured folder
"""
import hashlib
import hmac
import json
import os
import re
import urllib.parse
import secrets
import socket
import sys
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .log import log
from .util import APP_NAME, VERSION, human_size, human_time, is_http_url

MAX_FAILS = 8
LOCKOUT = 15 * 60


def new_token():
    return secrets.token_urlsafe(32)


def lan_ip():
    """This PC's address on the local network: the interface that holds the default route
    (the Wi-Fi/Ethernet to the router), not a VPN/WSL/Hyper-V adapter."""
    for probe in (("192.168.255.255", 1), ("8.8.8.8", 80), ("10.255.255.255", 1)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(probe)                     # UDP connect: picks a route, sends nothing
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith(("127.", "169.254.")):
                return ip
        except OSError:
            pass
    return "127.0.0.1"


def _rule_name(port):
    return f"PyDM remote (TCP {int(port)})"


def firewall_needed():
    return sys.platform.startswith("win")


def firewall_rule_ok(port):
    """True if Windows Firewall already lets phones on the local network reach PyDM."""
    if not firewall_needed():
        return True
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                            f"name={_rule_name(port)}"], capture_output=True, text=True,
                           timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0 and "Allow" in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def request_firewall_rule(port):
    """Ask Windows (one admin prompt) to allow PyDM's remote port from the local network only."""
    if not firewall_needed():
        return True
    import ctypes
    name = _rule_name(port)
    cmd = (f'/c netsh advfirewall firewall delete rule name="{name}" >nul 2>&1 & '
           f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow '
           f'protocol=TCP localport={int(port)} remoteip=localsubnet profile=any')
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, None, 0)
    return rc > 32                                   # > 32 = started; user may still say No


def _download_json(d):
    sec = (d.security or {}).get("level")
    return {"id": d.id, "name": d.filename, "status": d.status,
            "retrying": bool(d.retry_at), "progress": round(d.progress, 4),
            "size": human_size(d.expected_total) if d.expected_total else "",
            "done": human_size(d.downloaded), "speed": human_size(d.speed) + "/s"
            if d.is_running() else "", "eta": human_time(d.eta) if d.is_running() else "",
            "error": d.error, "category": d.category, "security": sec,
            "kind": d.kind}


def sendable(d):
    """Finished, still on disk, and not flagged as dangerous."""
    return (d.status == "Completed" and d.path and os.path.isfile(d.path) and
            (d.security or {}).get("level") != "danger")


class RemoteServer:
    def __init__(self, manager, port, lan=True, token=None):
        self.m = manager
        self.port = port
        self.lan = lan
        self.token = token
        self._fails = {}
        self._lock = threading.Lock()
        self.server = None

    def url(self):
        host = lan_ip() if self.lan else "127.0.0.1"
        return f"http://{host}:{self.port}/#t={self.token}"

    # ---- send to phone: signed, expiring per-file links (no app or login on the phone)
    def _sig(self, did, exp):
        return hmac.new(self.token.encode(), f"file:{did}:{exp}".encode(),
                        hashlib.sha256).hexdigest()[:40]

    def file_path(self, d, hours=24):
        exp = int(time.time() + hours * 3600)
        name = urllib.parse.quote(os.path.basename(d.path or d.filename or "file"))
        return f"/f/{d.id}/{name}?e={exp}&s={self._sig(d.id, exp)}"

    def file_link(self, d, hours=24):
        host = lan_ip() if self.lan else "127.0.0.1"
        return f"http://{host}:{self.port}{self.file_path(d, hours)}"

    def check_file(self, did, exp, sig):
        try:
            exp = int(exp)
        except (TypeError, ValueError):
            return False
        return exp > time.time() and bool(sig) and hmac.compare_digest(
            sig.encode(), self._sig(did, exp).encode())

    def _locked(self, ip):
        with self._lock:
            n, until = self._fails.get(ip, (0, 0))
            return until > time.time()

    def _fail(self, ip):
        with self._lock:
            n, until = self._fails.get(ip, (0, 0))
            n += 1
            self._fails[ip] = (n, time.time() + LOCKOUT if n >= MAX_FAILS else 0)

    def _ok(self, ip):
        with self._lock:
            self._fails.pop(ip, None)

    def start(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PyDM"
            sys_version = ""

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else (
                    body.encode() if isinstance(body, str) else json.dumps(body).encode())
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'self'; style-src 'self'; "
                                 "connect-src 'self'; img-src 'self' data:; "
                                 "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(data)

            def _auth(self):
                ip = self.client_address[0]
                if srv._locked(ip):
                    self._send(429, {"error": "Too many attempts. Try again later."})
                    return False
                h = self.headers.get("Authorization") or ""
                given = h[7:] if h.startswith("Bearer ") else ""
                if not srv.token or not given or not hmac.compare_digest(
                        given.encode(), srv.token.encode()):
                    srv._fail(ip)
                    self._send(401, {"error": "Not authorized — scan the QR code in PyDM "
                                              "again."})
                    return False
                srv._ok(ip)
                return True

            def _body(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 64 * 1024:
                    return None
                try:
                    data = json.loads(self.rfile.read(n))
                    return data if isinstance(data, dict) else None
                except ValueError:
                    return None

            def _send_file(self, d, head=False):
                size = os.path.getsize(d.path)
                rng = self.headers.get("Range", "")
                start, end, partial = 0, size - 1, False
                m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                if m and (m[1] or m[2]):
                    if m[1]:
                        start = int(m[1])
                        end = min(int(m[2]), size - 1) if m[2] else size - 1
                    else:
                        start = max(0, size - int(m[2]))
                    partial = True
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                name = os.path.basename(d.path)
                ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"; '
                                 f"filename*=UTF-8''{urllib.parse.quote(name)}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                if head:
                    return
                log.info("sent %s to %s (%s)", name, self.client_address[0],
                         human_size(end - start + 1))
                with open(d.path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    try:
                        while left > 0:
                            chunk = f.read(min(1 << 20, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except (ConnectionError, OSError):
                        pass

            def _file_route(self, head=False):
                ip = self.client_address[0]
                if srv._locked(ip):
                    return self._send(429, {"error": "Too many attempts. Try again later."})
                u = urllib.parse.urlsplit(self.path)
                parts = u.path.split("/")             # "", "f", id, name
                q = urllib.parse.parse_qs(u.query)
                did = parts[2] if len(parts) > 3 else ""
                if not srv.check_file(did, (q.get("e") or [""])[0], (q.get("s") or [""])[0]):
                    srv._fail(ip)
                    return self._send(403, "This link expired or isn't valid. Send it again "
                                           "from PyDM.", "text/plain; charset=utf-8")
                d = srv.m.get(did)
                if d is None or not sendable(d):
                    return self._send(404, "That file isn't available any more.",
                                      "text/plain; charset=utf-8")
                self._send_file(d, head)

            def do_HEAD(self):
                if self.path.startswith("/f/"):
                    return self._file_route(head=True)
                self._send(405, {"error": "method not allowed"})

            def do_GET(self):
                if self.path.startswith("/f/"):
                    return self._file_route()
                path = self.path.split("?")[0]
                if path == "/":
                    return self._send(200, PAGE, "text/html; charset=utf-8")
                if path == "/app.js":
                    return self._send(200, SCRIPT, "application/javascript; charset=utf-8")
                if path == "/app.css":
                    return self._send(200, STYLE, "text/css; charset=utf-8")
                if path == "/api/downloads":
                    if not self._auth():
                        return
                    items = []
                    for d in srv.m.visible():
                        j = _download_json(d)
                        if sendable(d):
                            j["save"] = srv.file_path(d, hours=6)
                        items.append(j)
                    speed = sum(d.speed for d in srv.m.visible() if d.is_running())
                    return self._send(200, {"app": APP_NAME, "version": VERSION,
                                            "speed": human_size(speed) + "/s",
                                            "downloads": items[::-1]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._auth():
                    return
                data = self._body()
                if data is None:
                    return self._send(400, {"error": "bad request"})
                path = self.path.split("?")[0]
                if path == "/api/add":
                    url = str(data.get("url", "")).strip()[:8192]
                    if not (is_http_url(url) or url.startswith(("ftp://", "ftps://"))):
                        return self._send(400, {"error": "Enter an http(s) or ftp link"})
                    d = srv.m.add(url, source="remote")
                    return self._send(200, {"ok": True, "id": d.id})
                if path == "/api/action":
                    d = srv.m.get(str(data.get("id", "")))
                    if not d or d.parent_id:
                        return self._send(404, {"error": "not found"})
                    act = data.get("action")
                    if act == "pause":
                        srv.m.pause(d)
                    elif act == "resume":
                        srv.m.resume(d)
                    elif act == "remove":
                        srv.m.remove(d, delete_files=False)
                    else:
                        return self._send(400, {"error": "unknown action"})
                    return self._send(200, {"ok": True})
                if path == "/api/all":
                    (srv.m.pause_all if data.get("action") == "pause" else srv.m.resume_all)()
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})

            def log_message(self, *a):
                pass

        bind = "0.0.0.0" if self.lan else "127.0.0.1"
        self.server = ThreadingHTTPServer((bind, self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12"><title>PyDM Remote</title>
<link rel="stylesheet" href="/app.css"></head><body>
<header><div class="brand"><span class="logo">↓</span> PyDM <small>remote</small></div>
<div id="speed">—</div></header>
<form id="add"><input id="url" type="url" inputmode="url" placeholder="Paste a download link"
autocomplete="off" required><button>Add</button></form>
<div class="bar"><button id="resumeAll" class="ghost">Resume all</button>
<button id="pauseAll" class="ghost">Pause all</button></div>
<p id="msg" hidden></p><main id="list"></main>
<script src="/app.js"></script></body></html>"""

STYLE = """*{box-sizing:border-box}body{margin:0;font:15px/1.4 -apple-system,Segoe UI,Inter,Roboto,
sans-serif;background:#0b0d12;color:#e9ecf3;padding:env(safe-area-inset-top) 14px 40px}
header{display:flex;justify-content:space-between;align-items:center;padding:18px 2px 12px}
.brand{font-weight:700;font-size:19px;display:flex;align-items:center;gap:8px}
.brand small{color:#8b94a9;font-weight:500;font-size:13px}.logo{display:inline-grid;
place-items:center;width:28px;height:28px;border-radius:8px;color:#fff;background:
linear-gradient(135deg,#7c6cff,#43c6f0)}#speed{color:#43c6f0;font-weight:600}
form{display:flex;gap:8px}input{flex:1;background:#141821;border:1px solid #2c3346;color:#e9ecf3;
border-radius:12px;padding:12px;font-size:15px;min-width:0}button{border:0;border-radius:12px;
padding:12px 16px;font-weight:650;color:#fff;background:linear-gradient(90deg,#7c6cff,#5b8cff)}
.bar{display:flex;gap:8px;margin:10px 0}.ghost{background:#141821;border:1px solid #2c3346;
color:#cfd5e2;flex:1}.card{background:#141821;border:1px solid #222837;border-radius:14px;
padding:12px 14px;margin:10px 0}.top{display:flex;justify-content:space-between;gap:10px}
.name{font-weight:650;word-break:break-all}.chip{font-size:12px;font-weight:650;padding:2px 9px;
border-radius:10px;white-space:nowrap;height:fit-content}.meta{color:#8b94a9;font-size:13px;
margin:4px 0 8px}.track{height:6px;background:#222837;border-radius:3px;overflow:hidden}
.fill{height:100%;background:linear-gradient(90deg,#7c6cff,#43c6f0)}.done .fill{background:#22c55e}
.err .fill{background:#f43f5e}.acts{display:flex;gap:6px;margin-top:10px}.acts button{flex:1;
padding:8px;font-size:13px;background:#1b2030;border:1px solid #2c3346;color:#cfd5e2}
.acts a.save{flex:2;text-align:center;padding:8px;font-size:13px;font-weight:650;color:#fff;
text-decoration:none;border-radius:12px;background:linear-gradient(90deg,#22c55e,#16a34a)}
#msg{background:#1b2030;border:1px solid #2c3346;border-radius:12px;padding:10px 12px}
.empty{color:#8b94a9;text-align:center;margin-top:40px}"""

SCRIPT = r"""(() => {
const h = new URLSearchParams(location.hash.slice(1));
if (h.get("t")) { localStorage.setItem("pydm_token", h.get("t")); history.replaceState(null, "", "/"); }
const token = localStorage.getItem("pydm_token") || "";
const $ = (id) => document.getElementById(id);
const colors = {Completed:"#22c55e",Downloading:"#43c6f0",Error:"#f43f5e",Paused:"#f5a524",
  Queued:"#8b94a9",Verifying:"#38bdf8",Merging:"#38bdf8",Extracting:"#38bdf8",Connecting:"#43c6f0"};
function say(t) { const m = $("msg"); m.textContent = t; m.hidden = !t; }
async function api(path, body) {
  const r = await fetch(path, {method: body ? "POST" : "GET", headers: {"Authorization": "Bearer " + token,
    "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; return e; }
function render(data) {
  $("speed").textContent = data.speed;
  const list = $("list"); list.replaceChildren();
  if (!data.downloads.length) { list.append(el("p", "empty", "No downloads yet")); return; }
  for (const d of data.downloads) {
    const c = el("div", "card" + (d.status === "Completed" ? " done" : d.status === "Error" && !d.retrying ? " err" : ""));
    const top = el("div", "top"); top.append(el("div", "name", d.name));
    const label = d.retrying ? "Retrying" : d.security === "danger" ? "Threat" : d.status;
    const chip = el("span", "chip", label); const col = d.security === "danger" ? "#f43f5e" : (colors[d.status] || "#8b94a9");
    chip.style.color = col; chip.style.background = col + "22"; top.append(chip); c.append(top);
    const meta = d.status === "Error" ? d.error : [d.size && (d.done + " of " + d.size), d.speed, d.eta && d.eta + " left"].filter(Boolean).join(" · ");
    c.append(el("div", "meta", meta || " "));
    const t = el("div", "track"), f = el("div", "fill"); f.style.width = (d.progress * 100).toFixed(1) + "%"; t.append(f); c.append(t);
    const acts = el("div", "acts");
    const btn = (txt, action) => { const b = el("button", "", txt); b.onclick = () => api("/api/action", {id: d.id, action}).then(load).catch(e => say(e.message)); acts.append(b); };
    if (["Downloading","Queued","Connecting"].includes(d.status)) btn("Pause", "pause");
    if (["Paused","Error"].includes(d.status)) btn("Resume", "resume");
    if (d.save) { const a = el("a", "save", "Save to phone"); a.href = d.save;
      a.setAttribute("download", ""); acts.prepend(a); }
    btn("Remove", "remove"); c.append(acts); list.append(c);
  }
}
async function load() { try { render(await api("/api/downloads")); say(""); } catch (e) { say(e.message); } }
$("add").onsubmit = async (e) => { e.preventDefault(); try { await api("/api/add", {url: $("url").value}); $("url").value = ""; load(); } catch (err) { say(err.message); } };
$("pauseAll").onclick = () => api("/api/all", {action: "pause"}).then(load);
$("resumeAll").onclick = () => api("/api/all", {action: "resume"}).then(load);
if (!token) say("Scan the QR code shown in PyDM → Settings → Remote to connect.");
load(); setInterval(load, 2000);
})();"""
