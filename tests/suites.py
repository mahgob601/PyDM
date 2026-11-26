"""All PyDM test suites. Each suite is a function taking the runner context `t`."""
import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
IS_WIN = sys.platform.startswith("win")


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def same(a, b):
    return os.path.exists(a) and os.path.exists(b) and md5(a) == md5(b)


# --------------------------------------------------------------------------- core engine & security
def suite_core(t):
    from pydm.core import security
    from pydm.core.bridge import Bridge
    from pydm.core.util import expand_pattern, safe_filename
    m = t.manager("core")
    B = t.url("core")
    sha = hashlib.sha256(open(t.p("srv/big.zip"), "rb").read()).hexdigest()
    ds = {"ok": m.add(B + "big.zip", checksum=("sha256", sha)),
          "bad": m.add(B + "small.pdf", checksum=("sha256", "0" * 64)),
          "disguised": m.add(B + "invoice.pdf"), "expired": m.add(B + "expired.zip"),
          "double": m.add(B + "report.pdf.exe"), "404": m.add(B + "nope.zip")}
    m.update_settings({"max_active": 6})
    for d in ds.values():
        t.wait(m, d)
    lvl = {k: (d.security or {}).get("level") for k, d in ds.items()}
    t.check("multi-connection download intact + checksum verified",
            ds["ok"].status == "Completed" and same(ds["ok"].path, t.p("srv/big.zip"))
            and "verified" in ds["ok"].security["findings"][0]["text"])
    t.check("wrong checksum flagged as danger", lvl["bad"] == "danger")
    t.check("exe disguised as .pdf flagged", lvl["disguised"] == "danger")
    t.check("web page saved as .zip flagged", lvl["expired"] == "caution")
    t.check("double extension flagged", lvl["double"] == "danger")
    t.check("404 gives a clear error, no retry", ds["404"].status == "Error" and not
            ds["404"].retry_at, ds["404"].error)
    from pydm.core.util import content_disposition_filename as cdf
    t.check("Content-Disposition: filename* (UTF-8) wins over ASCII fallback",
            cdf("attachment; filename=\"download\"; filename*=UTF-8''%D8%AA%D9%82.pdf")
            == "\u062a\u0642.pdf" and cdf('attachment; filename="a \\"q\\".zip"') == 'a "q".zip'
            and cdf("attachment; filename*=iso-8859-1'en'%A3.pdf") == "\u00a3.pdf"
            and cdf("attachment") is None)
    t.check("batch pattern expansion", expand_pattern("http://x/img[08-11].jpg")[-1] ==
            "http://x/img11.jpg" and len(expand_pattern("http://x/[a-c]/f[1-3]")) == 9)
    t.check("right-to-left trick removed and flagged",
            safe_filename("invoice‮fdp.exe") == "invoicefdp.exe" and
            security.pre_download_findings("invoice‮fdp.exe", "http://x/")[0]["level"]
            == "danger")
    d = m.add(B + "big.zip", filename="refresh.zip")
    t.wait_running(m, d, 0.6)
    m.pause(d)
    t.wait(m, d)
    m.refresh_url(d, B + "movie.mp4")
    m.resume(d)
    t.wait(m, d)
    t.check("new address for a different file is refused", d.status == "Error" and
            "different file" in d.error, d.error)
    m.refresh_url(d, B + "big.zip?mirror=2")
    m.resume(d)
    t.wait(m, d)
    t.check("new address for the same file resumes", d.status == "Completed" and
            same(d.path, t.p("srv/big.zip")))
    port = t.free_port()
    br = Bridge(port, m)
    br.start()

    def post(origin, host=None, url=B + "song.mp3"):
        h = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{port}"}
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(f"http://127.0.0.1:{port}/add",
                                     data=json.dumps({"url": url}).encode(), headers=h)
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
    t.check("bridge: no-origin / website / DNS-rebinding requests rejected",
            (post(None), post("https://evil.com"), post("chrome-extension://abc", "evil.com"))
            == (403, 403, 421))
    threading.Timer(0.5, lambda: br.resolve_pairing(br.pair_requests.get(), True)).start()
    first = post("chrome-extension://abc")
    t.check("bridge: pairing approval then trusted", first == 200 and
            post("chrome-extension://abc") == 200)
    threading.Timer(0.5, lambda: br.resolve_pairing(br.pair_requests.get(), False)).start()
    t.check("bridge: denied extension stays blocked",
            post("chrome-extension://zzz") == 403 and post("chrome-extension://zzz") == 403)
    br.stop()


# --------------------------------------------------------------------------- reliability
def suite_preview(t):
    """Instant preview: play while downloading; seeking fetches that part first."""
    import urllib.request
    from pydm.core.preview import PreviewServer, can_preview
    m = t.manager("preview")
    src = open(t.p("srv/f100.bin"), "rb").read()
    d = m.add(t.url("cdn") + "f100.bin", filename="movie.mp4", connections=4, auto_tune=False)
    t.wait_running(m, d, 1.0)
    t.check("preview offered for a video that's downloading", can_preview(d), d.status)
    pv = PreviewServer(m)
    url = pv.stream_url(d)

    def get(rng):
        t0 = time.time()
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": rng}),
                                    timeout=60) as r:
            return r.status, r.read(256 * 1024), time.time() - t0
    st, b, dt = get("bytes=0-")
    t.check("preview: plays from the start right away", st == 206 and b == src[:len(b)] and
            dt < 3, f"{dt:.2f}s")
    have = d.downloaded
    off = 90 * 1_000_000
    st, b, dt = get(f"bytes={off}-")
    t.check("preview: jump to 90% fetches that part first (seconds, not minutes)",
            st == 206 and b == src[off:off + len(b)] and dt < 5 and d.preview_seeks >= 1,
            f"{dt:.2f}s with {have / 2**20:.0f} MB downloaded, seeks={d.preview_seeks}")
    st, b, dt = get("bytes=-65536")
    t.check("preview: end of file (MP4 index) arrives fast too",
            st == 206 and b == src[-65536:] and dt < 5, f"{dt:.2f}s")
    bad = urllib.request.Request(url.replace(pv.secret, "x" * len(pv.secret)))
    try:
        urllib.request.urlopen(bad, timeout=5)
        ok = False
    except urllib.error.HTTPError as e:
        ok = e.code == 404
    t.check("preview: needs the per-session secret, only on 127.0.0.1",
            ok and url.startswith("http://127.0.0.1:"))
    t.wait(m, d, 200)
    st, b, dt = get("bytes=1000-")
    t.check("preview keeps working after the download completes and is renamed",
            d.status == "Completed" and b == src[1000:1000 + len(b)], d.status)
    pv.stop()


def suite_phone(t):
    """Share-to-PC: links posted to the private ntfy inbox topic become downloads."""
    import json as _json
    import queue as _queue
    import threading as _th
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pydm.core import inbox

    subs, published = [], []

    class Ntfy(BaseHTTPRequestHandler):           # enough of ntfy: publish + JSON stream
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            topic = self.path.strip("/")
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            tags = [x for x in (self.headers.get("Tags") or "").split(",") if x]
            ev = {"id": f"m{len(published)}", "event": "message", "topic": topic,
                  "message": body, "title": self.headers.get("Title", ""), "tags": tags}
            published.append(ev)
            for q in list(subs):
                q.put(ev)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            q = _queue.Queue()
            subs.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(b'{"event":"open"}\n')
            self.wfile.flush()
            try:
                while True:
                    try:
                        ev = q.get(timeout=1)
                    except _queue.Empty:
                        self.wfile.write(b'{"event":"keepalive"}\n')
                    else:
                        self.wfile.write(_json.dumps(ev).encode() + b"\n")
                    self.wfile.flush()
            except OSError:
                subs.remove(q)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
    srv.daemon_threads = True
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    server = f"http://127.0.0.1:{srv.server_address[1]}"
    m = t.manager("phone")
    topic = inbox.new_topic()
    t.check("inbox topic is long and random", len(topic) >= 24 and topic != inbox.new_topic(),
            topic)
    ib = inbox.Inbox(m, server, topic)
    ib.start()
    t0 = time.time()
    while ib.state != "listening" and time.time() - t0 < 10:
        time.sleep(0.05)

    def share(text):
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(f"{server}/{topic}", data=text.encode(),
                                                      method="POST"), timeout=5).read()
    url = t.url("core") + "small.pdf"
    share(f"Check this out {url} thanks!")         # how share sheets send text + link
    t0 = time.time()
    d = None
    while time.time() - t0 < 15 and d is None:
        d = m.find_url(url)
        time.sleep(0.1)
    t.check("link shared from the phone starts a download on the PC", d is not None and
            d.source == "phone", d and d.status)
    if d:
        t.wait(m, d, 60)
        t.check("shared download completes intact", d.status == "Completed" and
                md5(d.path) == md5(t.p("srv/small.pdf")), d.status)
    time.sleep(1)
    acks = [e for e in published if inbox.ACK_TAG in e["tags"]]
    t.check("phone gets a confirmation, and PyDM ignores its own confirmations",
            len(acks) == 1 and len(m.downloads) == 1, f"{len(acks)} acks, {len(m.downloads)} downloads")
    share(f"again {url}")
    time.sleep(2)
    t.check("the same link twice doesn't download twice", len(m.downloads) == 1,
            f"{len(m.downloads)} downloads")
    share("just some text, no link")
    time.sleep(1.5)
    t.check("a message without a link is answered, not downloaded",
            len(m.downloads) == 1 and any("no link" in e["title"] for e in published))
    t.check("inbox remembers where it was (no repeats after restart)",
            m.settings.get("share_since") == ib.since and ib.since)
    try:
        inbox.Inbox(m, "http://ntfy.example.com", topic).start()
        plain = True
    except ValueError:
        plain = False
    t.check("inbox refuses plain-http ntfy servers on the internet", not plain)
    ib.stop()
    srv.shutdown()

    # ---- send to phone: signed per-file links on the phone remote
    import socket as _so
    import urllib.request as _ur
    from pydm.core import remote
    with _so.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        port = _s.getsockname()[1]
    rs = remote.RemoteServer(m, port, lan=False, token=remote.new_token())
    rs.start()
    src = open(t.p("srv/small.pdf"), "rb").read()
    link = rs.file_link(d)
    with _ur.urlopen(link, timeout=10) as r:
        body, cd = r.read(), r.headers.get("Content-Disposition", "")
    t.check("send to phone: the QR link downloads the finished file intact",
            body == src and "attachment" in cd and "filename*=UTF-8''" in cd, cd)
    with _ur.urlopen(_ur.Request(link, headers={"Range": "bytes=100-"}), timeout=10) as r:
        t.check("send to phone: resumes mid-file (Range)", r.status == 206 and
                r.read() == src[100:])

    def status(u):
        try:
            with _ur.urlopen(u, timeout=10) as r:
                return r.status
        except _ur.HTTPError as e:
            return e.code
    sig = link.split("&s=")[1]
    forged = link.replace(sig, ("0" if sig[0] != "0" else "1") + sig[1:])
    expired = rs.file_link(d, hours=-1)
    t.check("send to phone: forged or expired links are refused",
            status(forged) == 403 and status(expired) == 403)
    sec = dict(d.security or {})
    d.security = dict(sec, level="danger")
    fresh = rs.file_link(d)
    t.check("send to phone: files flagged as dangerous are never sent", status(fresh) == 404)
    d.security = sec
    api = _ur.Request(f"http://127.0.0.1:{port}/api/downloads",
                      headers={"Authorization": "Bearer " + rs.token})
    with _ur.urlopen(api, timeout=10) as r:
        items = __import__("json").loads(r.read())["downloads"]
    save = next((i.get("save") for i in items if i["id"] == d.id), None)
    t.check("phone page offers 'Save to phone' for finished files",
            bool(save) and status(f"http://127.0.0.1:{port}{save}") == 200, save)
    rs.stop()


def suite_quota(t):
    """Internet quota guardian: warn, hold near the limit, continue on request, free hours."""
    import datetime as _dt
    from pydm.core import quota as Q
    D = _dt.datetime
    t.check("quota cycle: renewal day in this month or the last",
            Q.cycle_start(25, D(2026, 3, 10)) == D(2026, 2, 25) and
            Q.cycle_start(5, D(2026, 3, 10)) == D(2026, 3, 5) and
            Q.cycle_start(31, D(2026, 2, 28, 12)) == D(2026, 2, 28) and
            Q.next_renewal(31, D(2026, 1, 31, 9)) == D(2026, 2, 28))
    t.check("free hours window, including past midnight",
            Q.in_window("02:00", "08:00", D(2026, 1, 1, 3)) and
            not Q.in_window("02:00", "08:00", D(2026, 1, 1, 9)) and
            Q.in_window("23:00", "06:00", D(2026, 1, 1, 1)) and
            not Q.in_window("", "", D(2026, 1, 1, 1)))
    m = t.manager("quota", quota_enabled=True, quota_gb=0.05, quota_mode="pydm",
                  quota_stop_percent=95, quota_renew_day=1)
    m.quota = Q.Quota(m, os.path.join(t.dir("quota_state"), "quota.json"))
    d = m.add(t.url("cdn") + "f100.bin", filename="big.bin", connections=4, auto_tune=False)
    t0 = time.time()
    seen = []
    while time.time() - t0 < 60:
        m.schedule()
        while not m.events.empty():
            k, payload = m.events.get_nowait()
            if k == "quota":
                seen.append(payload[0])
        if m.quota.held() and not d.is_running():
            break
        time.sleep(0.1)
    used = m.quota.used()
    t.check("quota: warns at 80% and pauses downloads at the stop level",
            "caution" in seen and "danger" in seen and m.quota.held() and
            d.status == "Paused" and 0.95 * 50e6 <= used < 0.95 * 50e6 + 12e6,
            f"events={seen} status={d.status} used={used / 1e6:.1f} MB")
    m.schedule()
    time.sleep(1)
    t.check("quota: held downloads stay paused", d.status == "Paused" and not d.is_running())
    m.quota.allow_anyway()
    t.wait(m, d, 120)
    t.check("quota: 'continue anyway' resumes and finishes the file intact",
            d.status == "Completed" and md5(d.path) == md5(t.p("srv/f100.bin")), d.status)
    m.quota.set_used(10)
    t.check("quota: 'I've used 10 GB' correction matches the ISP's number",
            abs(m.quota.used() - 10e9) < 1e6, f"{m.quota.used() / 1e9:.3f} GB")
    n = m.quota.numbers()
    t.check("quota: daily budget and days left are computed",
            n["days_left"] > 0 and n["per_day"] >= 0 and n["limit"] == 50e6, str(
                {k: n[k] for k in ("days_left", "per_day")}))
    # big downloads wait for the free hours
    now = D.now()
    far = (now + _dt.timedelta(hours=3)).strftime("%H:%M")
    farther = (now + _dt.timedelta(hours=4)).strftime("%H:%M")
    m.update_settings({"quota_gb": 1000, "quota_free_start": far, "quota_free_end": farther,
                       "quota_big_wait": True, "quota_big_mb": 50})
    m.quota.state["override"] = False
    big = m.add(t.url("core") + "f100.bin", filename="later.bin")
    small = m.add(t.url("core") + "small.pdf", filename="now.pdf")
    t0 = time.time()
    while time.time() - t0 < 20 and small.status != "Completed":
        m.schedule()
        time.sleep(0.1)
    m.schedule()
    t0 = time.time()
    while time.time() - t0 < 10 and (big.is_running() or not getattr(big, "waits_free", 0)):
        m.schedule()
        time.sleep(0.1)
    t.check("quota: big downloads wait for the free hours, small ones go now",
            small.status == "Completed" and big.waits_free and not big.is_running() and
            big.downloaded < 50 * 2**20, f"small={small.status} big={big.status} "
            f"{big.downloaded / 2**20:.0f} MB")
    m.update_settings({"quota_free_start": now.strftime("%H:%M"), "quota_free_end": farther})
    t.wait(m, big, 120)
    t.check("quota: ...and start by themselves when the free hours begin",
            big.status == "Completed" and not big.waits_free, big.status)


def suite_reliability(t):
    # every connection goes silent mid-transfer (socket stays open): the watchdog must
    # reconnect long before the 60 s socket timeout configured here
    hm = t.manager("stall", timeout=60)
    h = hm.add(t.url("hang") + "f100.bin", connections=4, auto_tune=False)
    t0 = time.time()
    t.wait(hm, h, 120)
    el = time.time() - t0
    t.check("silent server: all connections redialed within seconds, file intact",
            h.status == "Completed" and h.stalls >= 1 and el < 30 and
            md5(h.path) == md5(t.p("srv/f100.bin")),
            f"{h.status} in {el:.0f}s, {h.stalls} stall recoveries, error={h.error}")
    h2 = hm.add(t.url("hang_some") + "f100.bin", filename="some.bin", connections=4,
                auto_tune=False)
    t0 = time.time()
    t.wait(hm, h2, 120)
    el = time.time() - t0
    t.check("some connections go silent: replaced, full speed, file intact",
            h2.status == "Completed" and h2.conn_restarts >= 2 and el < 20 and
            md5(h2.path) == md5(t.p("srv/f100.bin")),
            f"{h2.status} in {el:.0f}s, {h2.conn_restarts} connections replaced")
    h3 = hm.add(t.url("solo") + "f100.bin", filename="solo.bin", connections=4,
                auto_tune=True)
    t0 = time.time()
    t.wait(hm, h3, 150)
    el = time.time() - t0
    t.check("server punishes parallel connections: backs off to 1 and finishes",
            h3.status == "Completed" and h3.backoff and h3.target == 1 and el < 80 and
            md5(h3.path) == md5(t.p("srv/f100.bin")),
            f"{h3.status} in {el:.0f}s, backoff={h3.backoff}, target={h3.target}, "
            f"stalls={h3.stalls}, tuning={h3.tuning}")
    from pydm.core.util import human_time
    t.check("time left never shows nonsense while stalled",
            human_time(3e13) == "" and human_time(float("nan")) == "")
    m = t.manager("reliability", post_command=(
        'cmd /c copy nul "{folder}\\POST_{name}"' if IS_WIN else "touch {folder}/POST_{name}"))
    V = t.url("vid")
    g = m.add(V + "good.zip", extract=True)
    e = m.add(V + "evil.zip", extract=True)
    t.wait(m, g)
    t.wait(m, e)
    t.check("safe archive extracted", g.extracted_to and sorted(os.listdir(g.extracted_to)) ==
            ["data", "docs"])
    t.check("zip-slip archive refused, nothing escaped",
            any("Unsafe path" in f["text"] for f in e.security["findings"]) and
            not os.path.exists(os.path.join(os.path.dirname(t.dir("reliability")), "escape.txt")))
    time.sleep(1.5)
    t.check("post-download command ran", os.path.exists(os.path.join(os.path.dirname(g.path),
                                                                     "POST_good.zip")))
    if os.path.exists(t.p("vid/pack.7z")):
        s7 = m.add(V + "pack.7z", extract=True)
        t.wait(m, s7)
        t.check("7-Zip archive extracted", s7.extracted_to and os.path.exists(
            os.path.join(s7.extracted_to, "inner", "hello.txt")))
    s = m.add(V + "f100.bin", sequential=True, connections=4, auto_tune=False,
              filename="seq.bin")
    t.wait_running(m, s, 3)
    with s.lock:
        started = [x.start for x in s.segments if x.done > 0]
    t.check("streaming order downloads from the start", started and
            max(started) < 100_000_000 * 0.3, f"{len(started)} pieces started")
    m.pause(s)
    t.wait(m, s)
    src = t.p("vid/change.bin")
    shutil.copy(t.p("srv/f100.bin"), src)
    c = m.add(V + "change.bin", connections=4, auto_tune=False)
    t.wait_running(m, c, 2)
    m.pause(c)
    t.wait(m, c)
    with open(src, "r+b") as f:
        f.write(b"CHANGED")
    os.utime(src, (time.time() + 100, time.time() + 100))
    m.resume(c)
    t.wait(m, c)
    t.check("file changed while paused → restarted, new content", c.status == "Completed" and
            open(c.path, "rb").read(7) == b"CHANGED")
    port = t.free_port()
    p = t.start_bench("vid", port, "--per-conn", "3000")
    r = m.add(f"http://127.0.0.1:{port}/f100.bin", connections=4, auto_tune=False,
              filename="retry.bin")
    t.wait_running(m, r, 2.5)
    p.kill()
    p.wait()
    while r.is_running():
        m.schedule()
        time.sleep(0.1)
    kept = r.downloaded
    t.check("server dies → retry scheduled, progress kept", r.status == "Error" and r.retry_at
            and kept > 0, f"{r.error} · kept {kept // 2**20} MB")
    time.sleep(3)
    p2 = t.start_bench("vid", port, "--per-conn", "3000")
    t.wait(m, r, 200)
    t.check("server back → download finishes intact", r.status == "Completed" and
            same(r.path, t.p("srv/f100.bin")))
    p2.kill()


# --------------------------------------------------------------------------- features
def suite_features(t):
    from pydm.core import bandwidth, checksums, grabber, notify, remote, updater
    from pydm.core.bridge import Bridge
    from pydm.core.history import History
    m = t.manager("features")
    m.history = History(os.path.join(t.dir("features"), "h.db"))
    F = f"ftp://alice:s3cr3t@127.0.0.1:{t.ports['ftp']}/"
    d = m.add(F + "f100.bin", auto_tune=False, connections=8)
    el = t.wait(m, d)
    t.check("FTP multi-connection with login", d.status == "Completed" and
            same(d.path, t.p("srv/f100.bin")), f"{el:.1f}s, {len(d.segments)} parts")
    d2 = m.add(f"ftp://alice:wrong@127.0.0.1:{t.ports['ftp']}/big.zip")
    t.wait(m, d2)
    t.check("FTP wrong password → clear error", d2.status == "Error" and not d2.retry_at,
            d2.error)
    d4 = m.add(F + "f100.bin", connections=6, auto_tune=False, filename="ftp_resume.bin")
    t.wait_running(m, d4, 3)
    m.pause(d4)
    t.wait(m, d4)
    part = d4.downloaded
    m.resume(d4)
    t.wait(m, d4)
    t.check("FTP pause/resume", d4.status == "Completed" and 0 < part < 100_000_000 and
            same(d4.path, t.p("srv/f100.bin")), f"paused at {part // 2**20} MB")
    V = t.url("vid")
    d = m.add(V + "files/a.zip")
    t.wait(m, d)
    t.check("checksum found in SHA256SUMS and verified",
            any("verified automatically" in x["text"] for x in d.security["findings"]))
    d = m.add(V + "docs/manual.pdf")
    t.wait(m, d)
    t.check("checksum found in .sha256 sidecar and verified",
            any("verified automatically" in x["text"] for x in d.security["findings"]))
    t.check("checksum parser formats", checksums.parse("SHA256 (x.iso) = " + "a" * 64, "x.iso")
            == {"sha256": "a" * 64} and checksums.parse("d" * 64 + "  other.iso", "x.iso") == {})
    d = m.add(V + "files/a.zip", filename="a_copy.zip")
    t.wait(m, d)
    t.check("duplicate detected by hash",
            any("identical file" in x["text"] for x in d.security["findings"]))
    st = m.history.stats()
    t.check("history statistics", st["all"]["count"] >= 4 and len(st["daily"]) == 14)
    items = grabber.grab(V + "page.html", m.opener, depth=2)
    names = [i["url"].rsplit("/", 1)[-1] for i in items]
    t.check("page grabber: depth 2, same-site, de-duplicated", "deep.mp3" in names and
            "lvl3.iso" in names and names.count("a.zip") == 1, ", ".join(names))
    s = {"speed_rules": [{"days": list(range(7)), "start": "00:00", "end": "23:59",
                          "kbps": 300}]}
    t.check("time-of-day speed rule", bandwidth.Governor(s, lambda: 0, net_counter=None)
            .effective_limit() == 300 * 1024)
    clock, total, own = [0.0], [0], [0.0]
    g3 = bandwidth.Governor({"yield_to_apps": True}, lambda: own[0], net_counter=lambda:
                            total[0], clock=lambda: clock[0])
    for _ in range(20):
        clock[0] += 0.5
        total[0] += 5_000_000
        own[0] = 10_000_000
        g3.effective_limit()
    alone = g3.effective_limit()
    for _ in range(10):
        clock[0] += 0.5
        total[0] += 7_000_000
        lim = g3.effective_limit()
    t.check("yield to other apps", alone == 0 and 0 < lim < 10_000_000)
    try:
        import psutil
        before = psutil.net_io_counters().bytes_recv
        t.check("psutil network counters work here (needed for 'yield')", before > 0)
    except ImportError:
        t.check("psutil installed", False, "pip install psutil")
    tok = remote.new_token()
    rport = t.free_port()
    rs = remote.RemoteServer(m, rport, lan=False, token=tok)
    rs.start()

    def call(path, body=None, token=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        req = urllib.request.Request(f"http://127.0.0.1:{rport}{path}", headers=h,
                                     data=json.dumps(body).encode() if body else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
    rr = urllib.request.urlopen(f"http://127.0.0.1:{rport}/")
    t.check("remote: page served with strict CSP",
            "script-src 'self'" in rr.headers["Content-Security-Policy"])
    t.check("remote: API requires the token", call("/api/downloads")[0] == 401 and
            call("/api/downloads", token=tok)[0] == 200)
    code, j = call("/api/add", {"url": V + "files/b.zip"}, tok)
    t.check("remote: add from phone", code == 200 and m.get(j.get("id", "")) is not None)
    for _ in range(9):
        call("/api/downloads", token="wrong")
    t.check("remote: lockout after repeated wrong tokens", call("/api/downloads",
                                                                token=tok)[0] == 429)
    rs.stop()
    upd = t.dir("updates")
    tools = os.path.join(ROOT, "tools", "sign_release.py")
    subprocess.run([sys.executable, tools, "keygen"], cwd=upd, capture_output=True, check=True)
    with open(os.path.join(upd, "Setup.exe"), "wb") as f:
        f.write(os.urandom(300000))
    uport = t.free_port()
    subprocess.run([sys.executable, tools, "sign", "Setup.exe", "9.9",
                    f"http://127.0.0.1:{uport}/Setup.exe", "notes", "--key", "release_key.pem",
                    "--out", "manifest.json"], cwd=upd,
                   capture_output=True, check=True)
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(open(os.path.join(upd, "release_key.pem"),
                                                  "rb").read(), None)
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    us = t.start_bench(upd, uport)
    time.sleep(1)
    man = updater.check(f"http://127.0.0.1:{uport}/manifest.json", pub)
    t.check("updates: valid signed manifest accepted", man and man["version"] == "9.9")
    t.check("updates: '4.1' and '4.1.0' are the same version (no update loop)",
            updater.parse_version("4.1") == updater.parse_version("v4.1.0") and
            updater.parse_version("4.1.1") > updater.parse_version("4.1"))
    from pydm import release
    t.check("updates: official build points at signed GitHub releases",
            release.UPDATE_URL.startswith("https://github.com/") and
            len(base64.b64decode(release.PUBLIC_KEY)) == 32)
    orig = open(os.path.join(upd, "manifest.json"), "rb").read()
    open(os.path.join(upd, "manifest.json"), "wb").write(orig.replace(b"9.9", b"9.8"))
    try:
        updater.check(f"http://127.0.0.1:{uport}/manifest.json", pub)
        ok = False
    except updater.UpdateError as e:
        ok = "signature" in str(e)
    t.check("updates: tampered manifest refused", ok)
    open(os.path.join(upd, "manifest.json"), "wb").write(orig)
    d = m.add(man["url"], checksum=("sha256", man["sha256"]), source="update")
    t.wait(m, d)
    t.check("updates: installer verified against signed SHA-256", d.status == "Completed" and
            "verified" in d.security["findings"][0]["text"])
    us.kill()
    got = []

    class N(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers.get("Title"),
                        self.rfile.read(int(self.headers["Content-Length"])).decode()))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    nport = t.free_port()
    ns = http.server.HTTPServer(("127.0.0.1", nport), N)
    threading.Thread(target=ns.serve_forever, daemon=True).start()
    ok = notify.send(f"http://127.0.0.1:{nport}", "pydm-topic", "Download complete ✓", "x.mp4")
    t.check("phone notification (ntfy) sent", ok and got and got[0][0] == "/pydm-topic")
    ns.shutdown()
    bport = t.free_port()
    br = Bridge(bport, m)
    br.start()
    m.settings["trusted_origins"] = ["chrome-extension://ok"]

    def bpost(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{bport}{path}",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Origin": "chrome-extension://ok"})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
    c1 = bpost("/batch", {"links": [V + "files/a.zip", "javascript:x"]})
    c2 = bpost("/media", {"page_url": V + "page.html", "candidates": [{"url": V +
                                                                        "movie720.mp4"}]})
    t.check("bridge: batch + video endpoints", c1 == 200 and c2 == 200 and br.inbox.qsize() == 2)
    br.stop()


# --------------------------------------------------------------------------- streams
def suite_hls(t):
    m = t.manager("hls")
    V, S = t.url("vid"), t.url("vid_slow")
    cases = [("plain .ts", V + "hls_plain/index.m3u8", None, "video720"),
             ("AES-128 explicit IV", V + "hls_enc/index.m3u8", None, "video720"),
             ("AES-128 implicit IV", V + "hls_impl/index.m3u8", None, "video720"),
             ("fMP4", V + "hls_fmp4/index.m3u8", None, "video720"),
             ("master → best + separate audio", V + "master/master.m3u8", None, "video720"),
             ("master → 360p", V + "master/master.m3u8", 400, "video360")]
    for label, url, h, want in cases:
        d = m.add_stream(url, title=label.split()[0].replace("→", ""), height=h)
        t.wait(m, d)
        info = t.probe(d.path) if d.status == "Completed" else None
        t.check(f"HLS {label}", info and info[0] == 30 and "audio" in info[1] and
                want in info[1], f"{d.status} {info} {d.error}")
    d = m.add_stream(V + "drm.m3u8", title="drm")
    t.wait(m, d)
    t.check("HLS DRM stream refused", d.status == "Error" and "DRM" in d.error)
    import pydm.core.hls as H
    orig = H._decryptor
    H._decryptor = lambda: None
    try:
        d = m.add_stream(V + "hls_enc/index.m3u8", title="ffmpeg-decrypt")
        t.wait(m, d)
        info = t.probe(d.path) if d.status == "Completed" else None
        t.check("HLS decryption via ffmpeg (no cryptography pkg)", info and info[0] == 30,
                d.error)
    finally:
        H._decryptor = orig
    d = m.add_stream(S + "master/master.m3u8", title="restart")
    t.wait_running(m, d, 6)
    m.pause(d)
    t.wait(m, d)
    from pydm.core.engine import Download, Manager
    state = [x.to_dict() for x in m.snapshot()]
    m2 = Manager(persist=False)
    m2.update_settings(dict(m.settings))
    for item in state:
        m2.downloads.append(Download.from_dict(item, m2))
    d2 = m2.get(d.id)
    m2.resume(d2)
    t.wait(m2, d2, 300)
    info = t.probe(d2.path) if d2.status == "Completed" else None
    t.check("video+audio job: pause → app restart → resume", info and info[0] == 30 and
            "audio" in info[1], d2.error)


def suite_media(t):
    from pydm.core import media
    m = t.manager("media")
    V = t.url("vid")
    info = media.resolve(V + "page.html")
    opts = media.choices(info)
    d = m.add_media(info, opts[0])
    t.wait(m, d)
    pr = t.probe(d.path) if d.status == "Completed" else None
    t.check("yt-dlp: video on a web page", pr and pr[0] == 30, d.error)
    # only part of a video: 5 s → 9 s, fast (keyframe) and exact cuts
    for exact in (False, True):
        c = m.add_clip(V + "movie720.mp4", "clip", 5, 9, exact=exact)
        t.wait(m, c, 120)
        dur = None
        if c.status == "Completed" and t.ffprobe:
            import subprocess as _sp
            out = _sp.run([t.ffprobe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", c.path], capture_output=True, text=True).stdout
            dur = float(out.strip() or 0)
        tol = 0.35 if exact else 2.5
        t.check(f"only part of a video ({'exact' if exact else 'fast'} cut): 5s→9s",
                dur is not None and abs(dur - 4) <= tol and "[0m05s-0m09s]" in c.filename,
                f"{c.status} {c.filename} duration={dur} {c.error or ''}")
    try:
        media.check_clip(media.parse_time("2:00"), media.parse_time("1:00"))
        bad = False
    except media.MediaError:
        bad = True
    t.check("clip times: 12:30 / 1:02:03 / 90 understood, end-before-start refused",
            bad and media.parse_time("1:02:03") == 3723 and media.parse_time("12:30") == 750)
    info = media.resolve(V + "master/master.m3u8")
    opts = media.choices(info)
    t.check("yt-dlp: stream qualities listed", [o["label"] for o in opts][:2] == ["720p", "360p"],
            [o["label"] for o in opts])
    syn = {"title": "Merge Test: ünïcode/\\name?", "page_url": None, "formats": [
        {"id": "137", "ext": "mp4", "height": 720, "fps": 30, "vcodec": "avc1", "acodec": "none",
         "tbr": 3000, "size": None, "url": V + "v_only.mp4", "protocol": "https", "headers": {},
         "note": ""},
        {"id": "140", "ext": "m4a", "height": None, "fps": None, "vcodec": "none",
         "acodec": "mp4a", "tbr": 128, "size": None, "url": V + "a_only.m4a",
         "protocol": "https", "headers": {}, "note": ""}]}
    opts = media.choices(syn)
    d = m.add_media(syn, opts[0])
    t.wait(m, d)
    pr = t.probe(d.path) if d.status == "Completed" else None
    t.check("separate video + audio merged (YouTube-style)", pr and "audio" in pr[1] and
            "video720" in pr[1] and not [x for x in m.snapshot() if x.parent_id], d.error)
    d = m.add_media(syn, [o for o in opts if o["kind"] == "mp3"][0])
    t.wait(m, d)
    pr = t.probe(d.path) if d.status == "Completed" else None
    t.check("MP3 conversion", pr and pr[1] == ["audio"] and d.path.endswith(".mp3"), d.error)
    info = media.resolve(V + "dash/manifest.mpd")
    opts = media.choices(info)
    d = m.add_media(info, opts[0])
    t.wait(m, d)
    pr = t.probe(d.path) if d.status == "Completed" else None
    t.check("DASH stream via yt-dlp", pr and "audio" in pr[1], d.error)
    orig = media.resolve
    media.resolve = lambda url, **kw: {"title": "x", "page_url": url, "formats": [
        {"id": "18", "ext": "mp4", "height": 720, "fps": 30, "vcodec": "avc1", "acodec": "mp4a",
         "tbr": 1, "size": None, "url": V + "fresh_copy.mp4", "protocol": "https",
         "headers": {}, "note": ""}]}
    try:
        d = m.add(V + "expired_link.mp4", filename="expiring.mp4",
                  media={"page_url": "https://video.example/watch?v=1", "format": "18"})
        t.wait(m, d)
        t.check("expired video link refreshed automatically", d.status == "Completed" and
                same(d.path, t.p("vid/fresh_copy.mp4")), d.error)
    finally:
        media.resolve = orig


# --------------------------------------------------------------------------- speed
def suite_speed(t):
    def run(label, url, limit, **kw):
        m = t.manager("speed_" + label, antivirus_scan=False)
        d = m.add(url, **kw)
        t0 = time.time()
        t_xfer = None
        while time.time() - t0 < 300:                 # like t.wait, but note when bytes stop
            m.schedule()
            if t_xfer is None and d.status not in ("Queued", "Downloading", "Connecting"):
                t_xfer = time.time() - t0
            if not d.is_running() and (d.status in ("Completed", "Paused") or
                                       (d.status == "Error" and not d.retry_at)):
                break
            time.sleep(0.02)
        el = time.time() - t0
        if t_xfer:
            t.metrics[label + " · transfer only"] = round((d.total or 0) / t_xfer / 2**20, 1)
        mbps = (d.total or 0) / el / 2**20 if el else 0
        src = t.p("srv/" + url.rsplit("/", 1)[-1])
        ok = d.status == "Completed" and same(d.path, src)
        xfer = (d.total or 0) / t_xfer / 2**20 if t_xfer else 0
        # Raw local gates on the transfer itself; the integrity check after it (SHA-256 read,
        # plus Defender scanning the file on open on Windows) is reported, not gated.
        got = xfer if label.startswith("raw") else mbps
        t.check(f"speed: {label} ≥ {limit} MB/s", ok and got >= limit,
                f"{mbps:.1f} MB/s in {el:.1f}s incl. verification, transfer {xfer:.1f} MB/s, "
                f"{d.tuning}, target {d.target}")
        t.metrics[label] = round(mbps, 1)
        shutil.rmtree(os.path.dirname(d.path), ignore_errors=True)
    run("CDN, 1 MB/s per connection", t.url("cdn") + "f100.bin", 12)
    run("flaky server", t.url("flaky") + "f100.bin", 8)
    run("two mirrors", t.url("mirror1") + "f100.bin", 9, mirrors=[t.url("mirror2") + "f100.bin"])
    run("raw local (disk + engine, transfer)", t.url("raw") + "f400.bin", 80, connections=8,
        auto_tune=False)
    # Baseline without the engine: 8 threads reading the same file into memory and dropping it.
    # If this is slow too, the bench server (not PyDM) is the limit on this machine.
    import concurrent.futures
    import urllib.request
    size = os.path.getsize(t.p("srv/f400.bin"))
    part = size // 8

    def pull(i):
        req = urllib.request.Request(t.url("raw") + "f400.bin", headers={
            "Range": f"bytes={i * part}-{size - 1 if i == 7 else (i + 1) * part - 1}"})
        n = 0
        with urllib.request.urlopen(req, timeout=60) as r:
            while True:
                b = r.read(1 << 20)
                if not b:
                    return n
                n += len(b)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        got = sum(ex.map(pull, range(8)))
    base = got / (time.time() - t0) / 2**20
    t.metrics["bench server alone, 8 readers, no disk"] = round(base, 1)
    # Disk alone, through PyDM's own FileWriter: 8 threads, 8 regions, 256 KB writes.
    from pydm.core.engine import FileWriter, make_sparse
    blob = memoryview(os.urandom(256 * 1024))

    def disk(sparse):
        path = os.path.join(t.dir("speedlab"), f"w_{int(sparse)}.bin")
        open(path, "wb").close()
        if sparse:
            make_sparse(path)
        with open(path, "r+b") as f:
            f.truncate(size)
        fw = FileWriter(path)

        def region(i):
            pos, end = i * part, (size if i == 7 else (i + 1) * part)
            while pos < end:
                n = min(len(blob), end - pos)
                fw.write(pos, blob[:n])
                pos += n
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            list(ex.map(region, range(8)))
        fw.close()
        mbps = size / (time.time() - t0) / 2**20
        os.remove(path)
        return mbps
    for sparse in (True, False):
        t.metrics[f"disk alone, FileWriter, {'sparse' if sparse else 'plain'} file"] = \
            round(disk(sparse), 1)
    t.note("speed baseline", f"bench server alone: {base:.1f} MB/s; PyDM raw local: "
                             f"{t.metrics.get('raw local (disk + engine, transfer)')} MB/s")


# --------------------------------------------------------------------------- Windows only
def suite_windows(t):
    if not IS_WIN:
        t.skip("Windows-only checks", "not running on Windows")
        return
    from pydm.core import bandwidth, security
    from pydm.core.power import SleepInhibitor
    S = t.url("site")
    m = t.manager("windows", antivirus_scan=True, mark_of_the_web=True)
    defender = security._find_defender()
    t.check("Microsoft Defender command-line scanner found", bool(defender), defender)
    d = m.add(t.url("vid") + "files/a.zip")
    t.wait(m, d, 300)
    av = (d.security or {}).get("av") or {}
    t.check("Defender scan of a clean file → clean", av.get("status") == "clean",
            av.get("detail"))
    zi = (d.path or "") + ":Zone.Identifier"
    try:
        zone = open(zi, encoding="utf-8").read()
    except OSError as e:
        zone = f"<{e}>"
    t.check("Mark-of-the-Web written (SmartScreen/Office protection)",
            "ZoneId=3" in zone and "HostUrl=" in zone, zone.replace("\r\n", " | "))
    e = m.add(S + "eicar")
    t.wait(m, e, 300)
    sec = e.security or {}
    caught = (sec.get("level") == "danger" and any("threat" in f["text"].lower() or
                                                    "removed" in f["text"].lower()
                                                    for f in sec.get("findings", [])))
    t.check("EICAR test virus caught (scan or real-time protection)",
            caught or (e.status == "Error"), f"{e.status} · {e.error or sec.get('level')} · "
            f"file exists: {bool(e.path and os.path.exists(e.path))}")
    if e.status == "Error":
        t.check("blocked virus: clear antivirus message, never auto-retried",
                "antivirus" in (e.error or "").lower() and not e.retry_at, e.error)
    t.note("EICAR outcome", f"status={e.status}, error={e.error!r}, "
                            f"findings={[f['text'] for f in sec.get('findings', [])]}")
    # pre-allocation: full size up front, plain (not sparse — sparse writes are ~50x slower)
    sp = m.add(t.url("cdn") + "f100.bin", filename="prealloc.bin", connections=4,
               auto_tune=False)                     # throttled: still running when we look
    t.wait_running(m, sp, 1.5)
    try:
        st = os.stat(sp.temp_path)
        attrs, size = st.st_file_attributes, st.st_size
    except (OSError, TypeError) as ex:
        attrs, size = ex, None
    t.check("file pre-allocated to full size, plain NTFS (fast path)",
            size == sp.total and isinstance(attrs, int) and not attrs & 0x200,
            f"attributes={attrs}, size={size}, total={sp.total}, status={sp.status}")
    m.pause(sp)
    t.wait(m, sp)
    # sleep prevention
    inh = SleepInhibitor()
    inh.set(True)
    on = subprocess.run(["powercfg", "/requests"], capture_output=True, text=True).stdout
    inh.set(False)
    off = subprocess.run(["powercfg", "/requests"], capture_output=True, text=True).stdout
    exe = os.path.basename(sys.executable).lower()
    t.check("keeps the PC awake while downloading (powercfg /requests)",
            exe in on.lower() and exe not in off.lower(),
            on.split("SYSTEM:")[1].split("AWAYMODE")[0].strip()[:200] if "SYSTEM:" in on else on[:200])
    t.check("full-screen detection API answers", isinstance(bandwidth.fullscreen_busy(), bool))
    # Windows file-name traps
    names = {}
    for route in ("con", "spaces", "illegal", "dots", "unicode", "rtlo", "long"):
        d = m.add(S + "win/" + route)
        t.wait(m, d, 120)
        names[route] = d
    bad = '<>:"/\\|?*'
    okn = all(d.status == "Completed" and os.path.exists(d.path) for d in names.values())
    t.check("hostile file names download safely", okn,
            " · ".join(f"{k}={d.filename!r}/{d.status}" for k, d in names.items()))
    t.check("reserved name CON is renamed", names["con"].filename.upper() != "CON.TXT",
            names["con"].filename)
    t.check("no illegal characters / trailing dots",
            not any(c in names["illegal"].filename for c in bad) and
            not names["dots"].filename.endswith((".", " ")), f"{names['illegal'].filename!r}, "
            f"{names['dots'].filename!r}")
    t.check("'report.pdf      .exe' collapsed and flagged",
            names["spaces"].filename == "report.pdf.exe" and
            names["spaces"].security["level"] == "danger")
    t.check("Arabic/accented names preserved", names["unicode"].filename.startswith("تقرير"),
            names["unicode"].filename)
    t.check("RTLO name cleaned and flagged", names["rtlo"].security["level"] == "danger")
    t.check("very long name shortened", len(names["long"].filename) <= 200)
    ud = os.path.join(t.dir("windows"), "Téléchargements test ✓")
    m2 = t.manager("windows2", download_dir=ud, use_categories=True, antivirus_scan=False)
    d = m2.add(t.url("vid") + "docs/manual.pdf")
    t.wait(m2, d)
    t.check("download folder with spaces + Unicode", d.status == "Completed" and
            d.path.startswith(ud), d.path)
    code = ("import sys,time;sys.path.insert(0,r'%s');from PySide6.QtCore import QLockFile;"
            "l=QLockFile(r'%s');print(l.tryLock(100));sys.stdout.flush();time.sleep(%s)")
    lock = os.path.join(t.dir("windows"), "pydm.lock")
    first = subprocess.Popen([sys.executable, "-c", code % (ROOT, lock, 6)],
                             stdout=subprocess.PIPE, text=True)
    time.sleep(2)
    second = subprocess.run([sys.executable, "-c", code % (ROOT, lock, 0)],
                            capture_output=True, text=True).stdout.strip()
    first.kill()
    t.check("only one copy of PyDM can run", second == "False", second)


# --------------------------------------------------------------------------- GUI + packaging
def suite_gui(t):
    out = t.out_dir
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "gui_harness.py"),
                        t.url("vid"), t.url("vid_slow"), out, t.dir("gui")],
                       capture_output=True, text=True, timeout=240,
                       env=dict(os.environ, **({} if IS_WIN else {"QT_QPA_PLATFORM": "offscreen"})))
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {}
    t.check("app window, dialogs and settings render", res.get("shots", 0) >= 8,
            f"{res.get('shots')} screenshots · {r.stderr[-300:] if r.returncode else ''}")
    for k, v in res.get("checks", {}).items():
        t.check(k, v[0], v[1])
    home = t.dir("apphome")
    env = dict(os.environ, USERPROFILE=home, HOME=home)
    if not IS_WIN:
        env["QT_QPA_PLATFORM"] = "offscreen"
    p = subprocess.Popen([sys.executable, "-m", "pydm", "--hidden"], cwd=ROOT, env=env,
                         stderr=subprocess.PIPE, text=True)
    time.sleep(10)
    alive = p.poll() is None
    p.kill()
    err = p.stderr.read()[-400:]
    t.check("real app starts and keeps running (tray mode)", alive and "Traceback" not in err,
            err)


def suite_package(t):
    if not IS_WIN:
        t.skip("Windows .exe build", "not running on Windows")
        return
    build = t.dir("pyinstaller")
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed",
                        "--name", "PyDM", "--collect-submodules", "pydm",
                        "--collect-submodules", "yt_dlp", "--distpath",
                        os.path.join(build, "dist"), "--workpath", os.path.join(build, "work"),
                        "--specpath", build, os.path.join(ROOT, "run.py")],
                       capture_output=True, text=True, cwd=ROOT, timeout=1800)
    exe = os.path.join(build, "dist", "PyDM", "PyDM.exe")
    t.check("PyInstaller builds PyDM.exe", r.returncode == 0 and os.path.exists(exe),
            r.stderr[-400:] if r.returncode else exe)
    if not os.path.exists(exe):
        return
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in
               os.walk(os.path.dirname(exe)) for f in fs)
    t.metrics["exe folder MB"] = round(size / 2**20)
    home = t.dir("exehome")
    env = dict(os.environ, USERPROFILE=home)
    p = subprocess.Popen([exe, "--hidden"], env=env)
    time.sleep(12)
    alive = p.poll() is None
    p.kill()
    t.check("PyDM.exe starts and keeps running", alive, f"exit code {p.poll()}")
    outd = t.dir("execli")
    r = subprocess.run([exe, "--cli", t.url("vid") + "docs/manual.pdf", "-o", outd], env=env,
                       timeout=120)
    t.check("PyDM.exe --cli downloads a file", r.returncode == 0 and
            os.path.exists(os.path.join(outd, "manual.pdf")), f"exit {r.returncode}")
    r = subprocess.run([exe, "--cli", t.url("vid") + "page.html", "--video", "-o", outd],
                       env=env, timeout=240)
    t.check("PyDM.exe includes yt-dlp (video page download)", r.returncode == 0 and
            any(f.endswith(".mp4") for f in os.listdir(outd)), f"exit {r.returncode}")
    r = subprocess.run([exe, "--cli", t.url("vid") + "hls_enc/index.m3u8", "-n", "enc.mp4",
                        "-o", outd], env=env, timeout=240)
    t.check("PyDM.exe includes cryptography (encrypted stream)", r.returncode == 0 and
            os.path.exists(os.path.join(outd, "enc.mp4")), f"exit {r.returncode}")


# --------------------------------------------------------------------------- browser extension
def suite_extension(t):
    e2e = os.path.join(ROOT, "tests", "e2e")
    chrome = t.chrome
    if not chrome:
        t.skip("browser extension e2e", "Chrome for Testing not found")
        return
    inbox = os.path.join(t.dir("e2e"), "inbox.jsonl")
    env = dict(os.environ, E2E_EXT=os.path.join(ROOT, "extension"), E2E_DL=t.dir("e2e_dl"),
               E2E_PROFILE=t.dir("e2e_profile"), E2E_INBOX=inbox, E2E_CHROME=chrome,
               E2E_SITE=t.url("site").rstrip("/"), E2E_PYTHON=sys.executable,
               E2E_SERVERS=os.path.join(ROOT, "tests", "servers.py"),
               E2E_SHOTS=t.out_dir)
    r = subprocess.run(["node", "run.mjs"], cwd=e2e, env=env, capture_output=True, text=True,
                       timeout=400)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
    for ln in lines:
        ok, _, rest = ln.partition(" ")
        name, _, info = rest.partition(" :: ")
        t.check("extension: " + name.strip(), ok == "PASS", info.strip())
    if not lines:
        t.check("extension e2e ran", False, (r.stdout + r.stderr)[-600:])


SUITES = [("core", suite_core), ("reliability", suite_reliability),
          ("preview", suite_preview), ("phone", suite_phone),
          ("quota", suite_quota),
          ("features", suite_features), ("hls", suite_hls), ("media", suite_media),
          ("windows", suite_windows), ("extension", suite_extension), ("gui", suite_gui),
          ("speed", suite_speed), ("package", suite_package)]
