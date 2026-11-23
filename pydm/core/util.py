"""Shared constants and helpers."""
import ipaddress
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import socket

APP_NAME = "PyDM"
VERSION = "4.1"
APP_DIR = os.path.join(os.path.expanduser("~"), ".pydm")
STATE_FILE = os.path.join(APP_DIR, "downloads.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
TEMP_SUFFIX = ".pydm-part"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

CATEGORIES = {
    "Video": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg ts 3gp".split(),
    "Music": "mp3 wav flac aac ogg m4a wma opus".split(),
    "Compressed": "zip rar 7z tar gz bz2 xz tgz zst iso cab".split(),
    "Documents": "pdf doc docx xls xlsx ppt pptx txt rtf odt ods epub csv".split(),
    "Programs": "exe msi dmg pkg deb rpm apk appimage bin jar".split(),
}
CATEGORY_ORDER = list(CATEGORIES) + ["Other"]
DOWNLOAD_EXTS = {e for exts in CATEGORIES.values() for e in exts}

# Hidden characters used to disguise file names (e.g. "invoice‮fdp.exe")
HIDDEN_CHARS = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def ext_of(name):
    return os.path.splitext(name or "")[1].lower().lstrip(".")


def category_for(name):
    ext = ext_of(name)
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def human_size(n):
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(s):
    if s is None or s == float("inf"):
        return ""
    s = int(s)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def safe_filename(name):
    name = HIDDEN_CHARS.sub("", name or "")
    name = _BAD_CHARS.sub("_", name).strip().strip(". ")
    name = re.sub(r"\s+(\.[A-Za-z0-9]+)$", r"\1", name)   # "file.pdf      .exe" -> "file.pdf.exe"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[:200 - len(ext)] + ext
    return name or "download"


_CD_PARAM = re.compile(r""";\s*([!#$%&'*+.^_`|~0-9A-Za-z-]+\*?)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)""")


def content_disposition_filename(header):
    """File name from a Content-Disposition header (RFC 6266).

    `filename*=UTF-8''...` (RFC 5987) wins over the plain `filename=` that servers send as an
    ASCII fallback. Python's email parser returns whichever comes first, which turns
    "تقرير.pdf" into "download"."""
    if not header:
        return None
    params = {}
    for key, val in _CD_PARAM.findall(";" + header.split(";", 1)[-1] if ";" in header else ""):
        key, val = key.lower(), val.strip()
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            val = re.sub(r"\\(.)", r"\1", val[1:-1])
        params.setdefault(key, val)
    ext = params.get("filename*")
    if ext:
        m = re.match(r"([A-Za-z0-9!#$%&+^_`{}~-]*)'[^']*'(.*)$", ext)
        if m:
            charset = m.group(1) or "utf-8"
            try:
                name = urllib.parse.unquote(m.group(2), encoding=charset, errors="strict")
            except (LookupError, UnicodeDecodeError):
                name = urllib.parse.unquote(m.group(2), encoding="latin-1")
            if name.strip():
                return name
    name = params.get("filename")
    if name:
        if "%" in name:  # common non-standard habit: percent-encoded UTF-8 in filename=
            try:
                dec = urllib.parse.unquote(name, errors="strict")
                if dec != name:
                    name = dec
            except UnicodeDecodeError:
                pass
        return name.strip() or None
    return None


def filename_from_url(url):
    path = urllib.parse.urlsplit(url).path
    return urllib.parse.unquote(os.path.basename(path.rstrip("/")))


def unique_path(path):
    base, ext = os.path.splitext(path)
    cand, i = path, 1
    while os.path.exists(cand) or os.path.exists(cand + TEMP_SUFFIX):
        cand = f"{base} ({i}){ext}"
        i += 1
    return cand


def is_inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return os.path.commonpath([path, folder]) == folder


def open_path(p):
    if sys.platform.startswith("win"):
        os.startfile(p)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def reveal_in_folder(p):
    if p and os.path.exists(p):
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p])
            return
        p = os.path.dirname(p)
    if p:
        os.makedirs(p, exist_ok=True)
        open_path(p)


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        hints = {401: "login required", 403: "access denied — the link may have expired",
                 404: "file not found", 410: "link expired", 429: "too many requests"}
        hint = hints.get(e.code)
        return f"HTTP {e.code}" + (f" · {hint}" if hint else f" {e.reason}")
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if "CERTIFICATE_VERIFY_FAILED" in str(r):
            return "Untrusted TLS certificate — connection refused for your safety"
        if isinstance(r, OSError):
            return describe_error(r)
        if isinstance(r, socket.gaierror) or "Name or service" in str(r) or "getaddrinfo" in str(r):
            return "Network error: can't find the server (check the address or your connection)"
        return f"Network error: {r}"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "Connection timed out"
    if isinstance(e, socket.gaierror):
        return "Network error: can't find the server (check the address or your connection)"
    if isinstance(e, ConnectionRefusedError):
        return "Network error: server refused the connection"
    if isinstance(e, (ConnectionResetError, BrokenPipeError)):
        return "Network error: connection was reset"
    if isinstance(e, OSError) and e.strerror:
        return f"Network error: {e.strerror}"
    return str(e) or e.__class__.__name__


def is_http_url(url):
    try:
        p = urllib.parse.urlsplit((url or "").strip())
        return p.scheme in ("http", "https") and bool(p.hostname)
    except ValueError:
        return False


def is_private_host(host):
    """True for localhost / private / link-local addresses (literal or resolved)."""
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
            addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
        except (OSError, ValueError):
            return False
    return any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved for a in addrs)


_PATTERN = re.compile(r"\[(\d+)-(\d+)\]|\[([a-zA-Z])-([a-zA-Z])\]")


def expand_pattern(url, limit=5000):
    """file[001-120].jpg -> file001.jpg ... file120.jpg ; supports [a-z] too."""
    m = _PATTERN.search(url)
    if not m:
        return [url]
    if m.group(1) is not None:
        a, b = m.group(1), m.group(2)
        width = len(a) if a.startswith("0") else 0
        lo, hi = int(a), int(b)
        step = 1 if hi >= lo else -1
        values = [str(i).zfill(width) for i in range(lo, hi + step, step)]
    else:
        lo, hi = ord(m.group(3)), ord(m.group(4))
        step = 1 if hi >= lo else -1
        values = [chr(c) for c in range(lo, hi + step, step)]
    out = []
    for v in values:
        for u in expand_pattern(url[:m.start()] + v + url[m.end():], limit):
            out.append(u)
            if len(out) >= limit:
                return out
    return out
