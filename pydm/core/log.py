"""Diagnostic log: ~/.pydm/logs/pydm.log (rotating, 3 x 2 MB).

What gets logged: downloads starting/finishing/failing, what servers answer when something
goes wrong (status codes, refused or silent connections, reconnects), remote control,
updates and browser pairing. Never logged: secret tokens, cookies, auth headers or what's
inside files. URLs are logged without their query string, which often holds access keys.
"""
import logging
import logging.handlers
import os
import platform
import re
import sys
import urllib.parse

log = logging.getLogger("pydm")
log.addHandler(logging.NullHandler())
_path = None


def log_dir():
    from .util import APP_DIR
    return os.path.join(APP_DIR, "logs")


def setup(level=logging.INFO):
    """Start writing the log file. Safe to call more than once."""
    global _path
    if _path:
        return _path
    d = log_dir()
    os.makedirs(d, exist_ok=True)
    _path = os.path.join(d, "pydm.log")
    h = logging.handlers.RotatingFileHandler(_path, maxBytes=2 * 2**20, backupCount=3,
                                             encoding="utf-8", delay=True)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-12s %(message)s"))
    h.addFilter(_Redact())
    log.addHandler(h)
    log.setLevel(level)
    from .util import VERSION
    log.info("PyDM %s on %s %s, Python %s", VERSION, platform.system(), platform.release(),
             platform.python_version())
    return _path


def safe_url(url):
    """scheme://host/path only: query strings and user:password@ often carry secrets."""
    try:
        p = urllib.parse.urlsplit(url or "")
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
    except ValueError:
        return "<url>"


class _Redact(logging.Filter):
    """Last line of defence: blank anything that looks like a token or credential."""
    PAT = re.compile(r"(?i)(token|key|secret|password|passwd|auth|cookie|sig|signature)"
                     r"([\"'=:\s]+)([^\s&\"',;]{6,})")
    FRAG = re.compile(r"#t=[A-Za-z0-9_\-]+")

    def filter(self, record):
        msg = record.getMessage()
        clean = self.FRAG.sub("#t=…", self.PAT.sub(r"\1\2…", msg))
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def tail(lines=300):
    """Last lines of the current log, for 'Copy diagnostics'."""
    if not _path or not os.path.exists(_path):
        return ""
    with open(_path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 256 * 1024))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


def diagnostics(settings=None):
    """Plain-text report a user can paste into a bug report. No secrets."""
    from .util import VERSION
    out = [f"PyDM {VERSION}", f"OS: {platform.platform()}",
           f"Python: {platform.python_version()} ({'installed app' if getattr(sys, 'frozen', False) else 'source'})"]
    try:
        from . import media
        out.append(f"ffmpeg: {media.ffmpeg_path() or 'missing'}")
        out.append(f"JS runtime (YouTube): {media.js_runtime()[0] or 'missing'}")
        import yt_dlp
        out.append(f"yt-dlp: {yt_dlp.version.__version__}")
    except Exception as e:  # noqa: BLE001
        out.append(f"media: {e}")
    if settings:
        hidden = re.compile(r"token|key|secret|password|cookie|topic", re.I)
        shown = {k: ("…" if hidden.search(k) and v else v) for k, v in sorted(settings.items())
                 if not isinstance(v, (dict, list)) or k in ("host_limits",)}
        out.append("Settings: " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    out.append("")
    out.append("--- last log lines ---")
    out.append(tail())
    return "\n".join(out)
