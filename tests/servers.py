"""Test servers. Usage: python servers.py <site|ftp|pydm> <args…>"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def site(root, port):
    """Static files + dynamic attachment routes + in-memory EICAR (for the e2e/Defender tests)."""
    import http.server
    big = os.urandom(600_000)
    # EICAR is assembled at request time so no copy of it ever sits on disk in the fixtures
    eicar = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*").encode()
    names = {"/win/con": "CON.txt", "/win/spaces": "report.pdf      .exe",
             "/win/illegal": 'a<b>c:d"e|f?g*h.zip', "/win/dots": "trailing...",
             "/win/unicode": "تقرير ✓ résumé.pdf", "/win/rtlo": "invoice‮fdp.exe",
             "/win/long": ("x" * 240) + ".zip"}

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(s, *a, **k):
            super().__init__(*a, directory=root, **k)

        def end_headers(s):
            if s.path.startswith("/page.html"):
                s.send_header("Set-Cookie", "sess=abc123; Path=/")
            super().end_headers()

        def _attach(s, name, data, ctype="application/octet-stream"):
            from urllib.parse import quote
            s.send_response(200)
            s.send_header("Content-Type", ctype)
            s.send_header("Content-Disposition",
                          f"attachment; filename=\"download\"; filename*=UTF-8''{quote(name)}")
            s.send_header("Content-Length", str(len(data)))
            s.end_headers()
            s.wfile.write(data)

        def do_GET(s):
            p = s.path.split("?")[0]
            if p == "/eicar":
                return s._attach("eicar_test.com", eicar)
            if p in names:
                return s._attach(names[p], b"PDF-1.4 test" + os.urandom(2000))
            if p.startswith("/download") or p.startswith("/files/") or p.startswith("/docs/"):
                return s._attach("report-2026.zip" if p.startswith("/download")
                                 else p.rsplit("/", 1)[-1], big)
            if p.endswith(".m3u8"):
                d = open(os.path.join(root, p.lstrip("/")), "rb").read()
                s.send_response(200)
                s.send_header("Content-Type", "application/vnd.apple.mpegurl")
                s.send_header("Content-Length", str(len(d)))
                s.end_headers()
                s.wfile.write(d)
                return
            return super().do_GET()

        def log_message(s, *a):
            pass
    http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), H).serve_forever()


def ftp(root, port, rate):
    import logging
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler, ThrottledDTPHandler
    from pyftpdlib.servers import ThreadedFTPServer
    logging.disable(logging.CRITICAL)
    a = DummyAuthorizer()
    a.add_user("alice", "s3cr3t", root, perm="elr")
    a.add_anonymous(root)
    dtp = ThrottledDTPHandler
    dtp.write_limit = int(rate)

    class Handler(FTPHandler):
        pass
    Handler.authorizer, Handler.dtp_handler = a, dtp
    Handler.passive_ports = range(60000, 60200)
    srv = ThreadedFTPServer(("127.0.0.1", int(port)), Handler)
    srv.max_cons = srv.max_cons_per_ip = 256
    srv.serve_forever()


def pydm(port, inbox_path):
    """A PyDM bridge that auto-approves pairing and logs what it receives (extension e2e)."""
    from pydm.core.bridge import Bridge
    from pydm.core.engine import Manager
    m = Manager(persist=False)
    br = Bridge(int(port), m)
    br.start()
    log = open(inbox_path, "a", encoding="utf-8")
    while True:
        try:
            o = br.pair_requests.get(timeout=0.2)
            log.write(json.dumps({"type": "pairing", "origin": o}) + "\n")
            log.flush()
            br.resolve_pairing(o, True)
        except Exception:  # noqa: BLE001
            pass
        while not br.inbox.empty():
            log.write(json.dumps(br.inbox.get()) + "\n")
            log.flush()


if __name__ == "__main__":
    kind, args = sys.argv[1], sys.argv[2:]
    {"site": site, "ftp": ftp, "pydm": pydm}[kind](*args)
