"""Share links from your phone to PyDM, from anywhere.

PyDM listens to a private ntfy topic (its "inbox"). On the phone, in any app, tap
Share → ntfy → that topic: the link travels through ntfy to the PC, which starts the download
and sends a confirmation back. No port forwarding, no Wi-Fi requirement.

The topic name is the secret: anyone who knows it can queue downloads (never open them), so
it is long and random, can be regenerated, and incoming links are rate-limited.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from . import notify
from .log import log, safe_url

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
ACK_TAG = "pydm_ack"
MAX_LINKS = 30              # per 10 minutes
STREAM_TIMEOUT = 90         # ntfy sends a keepalive every ~45 s


def new_topic():
    return "pydm-in-" + notify.new_topic().split("-", 1)[1] + notify.new_topic()[-6:]


def extract_urls(text):
    out = []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(").,;!?]}'\"")
        if u not in out:
            out.append(u)
    return out


class Inbox:
    def __init__(self, manager, server, topic, since="", on_added=None, opener=None):
        self.m = manager
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.topic = topic
        self.since = since or ""
        self.on_added = on_added
        self.opener = opener or urllib.request.build_opener()
        self.stop_event = threading.Event()
        self.recent = []
        self.state = "starting"
        self.thread = None
        self._resp = None

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        scheme = urllib.parse.urlsplit(self.server).scheme
        if scheme != "https" and not self.server.startswith(("http://127.", "http://localhost")):
            raise ValueError("the ntfy server must use https://")
        self.thread = threading.Thread(target=self._loop, name="share-inbox", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        r = self._resp
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- listening
    def _loop(self):
        delay = 2
        while not self.stop_event.is_set():
            try:
                self._listen()
                delay = 2
            except Exception as e:  # noqa: BLE001
                if self.stop_event.is_set():
                    break
                self.state = f"reconnecting ({e.__class__.__name__})"
                log.info("share inbox: %s — retrying in %ss", e, delay)
                self.stop_event.wait(delay)
                delay = min(delay * 2, 120)

    def _listen(self):
        q = {"since": self.since} if self.since else {}
        url = f"{self.server}/{urllib.parse.quote(self.topic)}/json"
        if q:
            url += "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"Accept": "application/x-ndjson"})
        with self.opener.open(req, timeout=STREAM_TIMEOUT) as r:
            self._resp = r
            self.state = "listening"
            log.info("share inbox listening on %s", safe_url(self.server))
            for raw in r:
                if self.stop_event.is_set():
                    return
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if ev.get("event") == "message":
                    self.since = ev.get("id") or self.since
                    self._handle(ev)
                    self.m.update_settings({"share_since": self.since})
        self._resp = None

    # ---------------------------------------------------------------- handling
    def _handle(self, ev):
        if ACK_TAG in (ev.get("tags") or []):
            return                                   # our own confirmation
        urls = extract_urls(" ".join(str(ev.get(k) or "") for k in ("message", "title", "click")))
        if not urls:
            self._ack("PyDM: no link found", "Share a link (http…) to download it.")
            return
        now = time.time()
        self.recent = [t for t in self.recent if now - t < 600]
        for u in urls:
            if len(self.recent) >= MAX_LINKS:
                log.warning("share inbox: rate limit reached, ignoring %s", safe_url(u))
                self._ack("PyDM: too many links", "Wait a few minutes and share again.")
                break
            self.recent.append(now)
            old = self.m.find_url(u)
            if old is not None:                      # e.g. re-delivered after a restart
                self._ack("Already in PyDM", old.filename or u)
                continue
            try:
                d = self.add(u)
            except Exception as e:  # noqa: BLE001
                log.warning("share inbox: couldn't add %s: %s", safe_url(u), e)
                self._ack("PyDM couldn't add that link", str(e)[:200])
                continue
            log.info("share inbox: queued %s", safe_url(u))
            self._ack(f"⬇ Downloading on your PC", d.filename or u)
            if self.on_added:
                self.on_added(d)

    def add(self, url):
        """Direct files go straight in; video pages (YouTube etc.) get the best quality."""
        from . import media
        from .util import DOWNLOAD_EXTS, ext_of
        path = urllib.parse.urlsplit(url).path.lower()
        if path.endswith(".m3u8"):
            return self.m.add_stream(url, title="video", source="phone")
        if ext_of(path) in DOWNLOAD_EXTS:
            return self.m.add(url, source="phone")
        try:
            info = media.resolve(url)
            opts = media.choices(info, bool(media.ffmpeg_path(self.m.settings)))
        except Exception:  # noqa: BLE001
            opts = None                              # not a video page: plain download
        if opts:
            want = int(self.m.settings.get("phone_video_height") or 1080)
            fits = [o for o in opts if o.get("height") and o["height"] <= want]
            return self.m.add_media(info, fits[0] if fits else opts[0], source="phone")
        return self.m.add(url, source="phone")

    def _ack(self, title, text):
        s = self.m.settings
        if s.get("ntfy_enabled") and s.get("ntfy_topic"):
            notify.send_async(s.get("ntfy_server"), s["ntfy_topic"], title, text,
                              opener=self.opener)
        else:                                        # no notification topic: answer in the inbox
            notify.send_async(self.server, self.topic, title, text, tags=ACK_TAG,
                              opener=self.opener)
