"""Realistic test server: keep-alive, Range, ETag/If-Range, per-connection and global
throttling, connection limit (503), latency, and a share of randomly slow connections."""
import argparse, http.server, os, random, re, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("root"); ap.add_argument("port", type=int)
ap.add_argument("--per-conn", type=float, default=0, help="KB/s per connection, 0=unlimited")
ap.add_argument("--global-rate", type=float, default=0, help="KB/s for whole server")
ap.add_argument("--max-conns", type=int, default=0, help="concurrent transfers, then 503")
ap.add_argument("--slow", type=float, default=0, help="fraction of slow connections")
ap.add_argument("--latency", type=float, default=0, help="ms before each response")
ap.add_argument("--norange", action="store_true")
ap.add_argument("--hang", type=int, default=0,
                help="the first N responses go silent (connection kept open) after 1 MB")
A = ap.parse_args()

active = 0
hung = 0
lock = threading.Lock()
g_tokens, g_last = 0.0, time.monotonic()


def global_take(n):
    global g_tokens, g_last
    if A.global_rate <= 0:
        return
    rate = A.global_rate * 1024
    while True:
        with lock:
            now = time.monotonic()
            g_tokens = min(rate * 0.2, g_tokens + (now - g_last) * rate)
            g_last = now
            if g_tokens >= n:
                g_tokens -= n
                return
            wait = (n - g_tokens) / rate
        time.sleep(min(wait, 0.05))


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.slow = random.random() < A.slow

    def do_GET(self):
        global active
        if A.latency:
            time.sleep(A.latency / 1000)
        path = self.path.split("?")[0]
        if path.startswith("/redir"):
            self.send_response(302); self.send_header("Location", "/big.zip")
            self.send_header("Content-Length", "0"); self.end_headers(); return
        p = os.path.join(A.root, path.lstrip("/"))
        if not os.path.isfile(p):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        st = os.stat(p)
        size, etag = st.st_size, f'"{st.st_size:x}-{int(st.st_mtime):x}"'
        rng = self.headers.get("Range"); ifr = self.headers.get("If-Range")
        s, e, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d+)-(\d*)", rng or "")
        if m and not A.norange and (not ifr or ifr == etag):
            s = int(m[1]); e = min(int(m[2]) if m[2] else size - 1, size - 1); code = 206
        with lock:
            over = A.max_conns and active >= A.max_conns and (e - s) > 0
            if not over:
                active += 1
        if over:
            self.send_response(503); self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0"); self.end_headers(); return
        try:
            self.send_response(code)
            self.send_header("Content-Length", str(e - s + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {s}-{e}/{size}")
            self.send_header("Accept-Ranges", "bytes"); self.send_header("ETag", etag)
            self.end_headers()
            rate = 150 if self.slow else A.per_conn
            global hung
            with lock:
                hang = hung < A.hang
                hung += hang
            with open(p, "rb") as f:
                if hang:                       # send a little, then nothing, never closing
                    f.seek(s)
                    self.wfile.write(f.read(min(1 << 20, e - s + 1)))
                    self.wfile.flush()
                    time.sleep(3600)
                    return
                if rate <= 0 and A.global_rate <= 0:
                    self.wfile.flush()
                    self.connection.sendfile(f, s, e - s + 1)
                    return
                f.seek(s); left = e - s + 1; t0 = time.monotonic(); sent = 0
                while left > 0:
                    b = f.read(min(16384, left))
                    global_take(len(b))
                    self.wfile.write(b); left -= len(b); sent += len(b)
                    if rate > 0:
                        ahead = sent / (rate * 1024) - (time.monotonic() - t0)
                        if ahead > 0:
                            time.sleep(ahead)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            with lock:
                active -= 1

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", A.port), H)
srv.daemon_threads = True
srv.request_queue_size = 256
srv.serve_forever()
