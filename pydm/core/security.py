"""Security checks run before and after every download.

Before:  hidden/bidi characters in names, double extensions, programs served over plain
         HTTP, links that point into the local network.
After:   real file type from magic bytes (catches "invoice.pdf" that is really an .exe,
         or an HTML error page saved as a .zip), checksum verification, Windows
         Mark-of-the-Web, antivirus scan, optional VirusTotal hash lookup.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from .util import (DOWNLOAD_EXTS, HIDDEN_CHARS, ext_of, is_private_host,
                   DEFAULT_UA)

LEVELS = {"safe": 0, "info": 1, "caution": 2, "danger": 3}

DANGEROUS_EXTS = set("""exe msi msp bat cmd com scr pif ps1 psm1 vbs vbe js jse wsf wsh hta
cpl msc lnk reg jar dll sys apk dmg pkg app sh command appimage run deb rpm iso img vhd
vhdx xll docm xlsm pptm one url scf inf""".split())
CONTENT_EXTS = (DOWNLOAD_EXTS | {"jpg", "jpeg", "png", "gif", "txt", "html"}) - DANGEROUS_EXTS
EXECUTABLE_KINDS = {"windows-exe", "linux-exe", "mac-exe", "script"}
KIND_LABEL = {"windows-exe": "Windows program", "linux-exe": "Linux program",
              "mac-exe": "macOS program", "script": "script", "html": "web page",
              "ole": "Office/Installer file", "zip": "ZIP archive"}

SIGNATURES = [
    (0, b"MZ", "windows-exe"), (0, b"\x7fELF", "linux-exe"),
    (0, b"\xcf\xfa\xed\xfe", "mac-exe"), (0, b"\xce\xfa\xed\xfe", "mac-exe"),
    (0, b"\xfe\xed\xfa\xcf", "mac-exe"), (0, b"\xfe\xed\xfa\xce", "mac-exe"),
    (0, b"\xca\xfe\xba\xbe", "mac-exe"), (0, b"#!", "script"),
    (0, b"%PDF", "pdf"), (0, b"PK\x03\x04", "zip"), (0, b"PK\x05\x06", "zip"),
    (0, b"Rar!\x1a\x07", "rar"), (0, b"7z\xbc\xaf\x27\x1c", "7z"), (0, b"\x1f\x8b", "gzip"),
    (0, b"BZh", "bzip2"), (0, b"\xfd7zXZ\x00", "xz"), (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (257, b"ustar", "tar"), (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
    (4, b"ftyp", "mp4"), (0, b"\x1a\x45\xdf\xa3", "matroska"), (0, b"RIFF", "riff"),
    (0, b"OggS", "ogg"), (0, b"fLaC", "flac"), (0, b"ID3", "mp3"), (0, b"\x89PNG", "png"),
    (0, b"\xff\xd8\xff", "jpeg"), (0, b"GIF8", "gif"), (0, b"FLV", "flv"),
    (0, b"\x30\x26\xb2\x75", "asf"), (0, b"\x00\x00\x01\xba", "mpeg"),
    (0, b"\x00\x00\x01\xb3", "mpeg"), (0x8001, b"CD001", "iso"), (0, b"xar!", "xar"),
    (0, b"!<arch>", "deb"), (0, b"\xed\xab\xee\xdb", "rpm"),
]
COMPATIBLE = {
    "windows-exe": {"exe", "dll", "sys", "scr", "cpl", "com", "efi", "ocx", "mui"},
    "linux-exe": {"bin", "appimage", "so", "run", "elf", ""},
    "mac-exe": {"dylib", "bin", "class", ""},
    "script": {"sh", "py", "pl", "rb", "bash", "run", "command", "txt"},
    "pdf": {"pdf"}, "rar": {"rar"}, "7z": {"7z"}, "gzip": {"gz", "tgz"},
    "bzip2": {"bz2", "tbz2"}, "xz": {"xz", "txz"}, "zstd": {"zst"}, "tar": {"tar"},
    "zip": {"zip", "docx", "xlsx", "pptx", "jar", "apk", "epub", "odt", "ods", "odp", "xpi",
            "crx", "whl", "nupkg", "ipa", "appx", "msix", "vsix", "kmz", "3mf", "docm", "xlsm",
            "pptm"},
    "ole": {"msi", "msp", "doc", "xls", "ppt", "msg"},
    "mp4": {"mp4", "m4v", "mov", "m4a", "3gp", "heic", "avif"},
    "matroska": {"mkv", "webm", "mka"}, "riff": {"avi", "wav", "webp"},
    "ogg": {"ogg", "opus", "oga", "ogv"}, "flac": {"flac"}, "mp3": {"mp3"},
    "png": {"png"}, "jpeg": {"jpg", "jpeg"}, "gif": {"gif"}, "flv": {"flv"},
    "asf": {"wmv", "wma", "asf"}, "mpeg": {"mpg", "mpeg", "ts", "m2ts", "vob"},
    "iso": {"iso"}, "xar": {"pkg", "xar"}, "deb": {"deb"}, "rpm": {"rpm"},
    "html": {"html", "htm", "xhtml", "php", "asp", "aspx", "jsp", "mhtml"},
}
KNOWN_EXTS = set().union(*COMPATIBLE.values()) - {""}


def finding(level, text):
    return {"level": level, "text": text}


def overall(findings):
    worst = max((LEVELS[f["level"]] for f in findings), default=0)
    return {v: k for k, v in LEVELS.items()}[worst] if worst >= 2 else "safe"


# --------------------------------------------------------------------------- before
def double_extension(name):
    parts = name.lower().split(".")
    return len(parts) >= 3 and parts[-2] in CONTENT_EXTS and parts[-1] in DANGEROUS_EXTS


def name_findings(raw_name):
    out = []
    name = raw_name or ""
    if HIDDEN_CHARS.search(name):
        out.append(finding("danger", "The file name contains hidden characters that can make "
                                     "a program look like a document (they were removed)."))
    clean = HIDDEN_CHARS.sub("", name)
    if double_extension(clean):
        out.append(finding("danger", f"Double extension “{'.'.join(clean.split('.')[-2:])}” — "
                                     "a classic trick to disguise a program as a document."))
    return out


def pre_download_findings(raw_name, url):
    out = name_findings(raw_name)
    clean = HIDDEN_CHARS.sub("", raw_name or "")
    parts = urllib.parse.urlsplit(url)
    if ext_of(clean) in DANGEROUS_EXTS and parts.scheme == "http":
        out.append(finding("caution", "Program file delivered over unencrypted HTTP — it could "
                                      "be altered in transit. Prefer an https:// link or verify "
                                      "its checksum."))
    try:
        private = is_private_host(parts.hostname)
    except Exception:  # noqa: BLE001
        private = False
    if private:
        out.append(finding("caution", "This link points into your local network or this "
                                      "computer."))
    return out


# --------------------------------------------------------------------------- after
def sniff(path):
    try:
        with open(path, "rb") as f:
            head = f.read(0x8010)
    except OSError:
        return None
    for off, sig, kind in SIGNATURES:
        if head[off:off + len(sig)] == sig:
            return kind
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    text = head[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith((b"<!doctype html", b"<html")) or b"<head" in text[:512]:
        return "html"
    return None


def compute_hashes(path, algos=("sha256",)):
    hs = {a: hashlib.new(a) for a in set(algos)}
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            for h in hs.values():
                h.update(block)
    return {a: h.hexdigest() for a, h in hs.items()}


def content_findings(path, name, final_url):
    out = []
    kind = sniff(path)
    ext = ext_of(name)
    if kind in EXECUTABLE_KINDS and ext not in COMPATIBLE[kind]:
        out.append(finding("danger", f"This file is actually a {KIND_LABEL[kind]} but is named "
                                     f"“.{ext or 'no extension'}”. It is very likely disguised "
                                     "malware — do not open it."))
    elif kind == "html" and ext not in COMPATIBLE["html"]:
        out.append(finding("caution", "The server sent a web page instead of the file. The link "
                                      "has probably expired or needs you to log in."))
    elif kind and ext in KNOWN_EXTS and ext not in COMPATIBLE.get(kind, set()):
        out.append(finding("caution", f"The content looks like “{KIND_LABEL.get(kind, kind)}”, "
                                      f"which doesn't match the .{ext} extension."))
    if ext in DANGEROUS_EXTS or kind in EXECUTABLE_KINDS:
        if urllib.parse.urlsplit(final_url).scheme == "http":
            out.append(finding("caution", "Program received over unencrypted HTTP — verify the "
                                          "checksum before running it."))
        else:
            out.append(finding("info", "This is a program or installer. Run it only if you "
                                       "trust the website it came from."))
    return kind, out


def write_mark_of_the_web(path, url, referrer=None):
    """Windows: tag the file as 'from the Internet' so SmartScreen / Office Protected View
    treat it with the same caution as a browser download."""
    if not sys.platform.startswith("win"):
        return False

    def strip_creds(u):
        p = urllib.parse.urlsplit(u)
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, ""))
    lines = ["[ZoneTransfer]", "ZoneId=3"]
    if referrer:
        lines.append(f"ReferrerUrl={strip_creds(referrer)}")
    lines.append(f"HostUrl={strip_creds(url)}")
    try:
        with open(path + ":Zone.Identifier", "w", encoding="utf-8") as f:
            f.write("\r\n".join(lines) + "\r\n")
        return True
    except OSError:
        return False


def _find_defender():
    pd = os.environ.get("ProgramData", r"C:\ProgramData")
    plat = os.path.join(pd, "Microsoft", "Windows Defender", "Platform")
    cands = []
    if os.path.isdir(plat):
        for v in sorted(os.listdir(plat), reverse=True):
            cands.append(os.path.join(plat, v, "MpCmdRun.exe"))
    cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                              "Windows Defender", "MpCmdRun.exe"))
    return next((c for c in cands if os.path.isfile(c)), None)


def antivirus_scan(path, timeout=600):
    """Returns (status, detail). status: clean | threat | removed | unavailable | error"""
    try:
        if sys.platform.startswith("win"):
            exe = _find_defender()
            if not exe:
                return "unavailable", "Microsoft Defender not found"
            r = subprocess.run([exe, "-Scan", "-ScanType", "3", "-File", path],
                               capture_output=True, timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            name = "Microsoft Defender"
            threat = r.returncode == 2
            ok = r.returncode == 0
        else:
            exe = shutil.which("clamdscan") or shutil.which("clamscan")
            if not exe:
                return "unavailable", "No antivirus scanner found (install ClamAV to enable)"
            r = subprocess.run([exe, "--no-summary", path], capture_output=True, timeout=timeout)
            name = "ClamAV"
            threat = r.returncode == 1
            ok = r.returncode == 0
        if threat:
            if not os.path.exists(path):
                return "removed", f"{name} found a threat and removed the file"
            return "threat", f"{name} found a threat in this file"
        if ok:
            return "clean", f"{name}: no threats found"
        return "error", f"{name} scan failed (code {r.returncode})"
    except subprocess.TimeoutExpired:
        return "error", "Antivirus scan timed out"
    except OSError as e:
        return "error", f"Antivirus scan failed: {e}"


def virustotal_lookup(sha256, api_key, opener=None):
    """Looks up the file *hash* only — the file itself is never uploaded."""
    req = urllib.request.Request(f"https://www.virustotal.com/api/v3/files/{sha256}",
                                 headers={"x-apikey": api_key, "User-Agent": DEFAULT_UA})
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "unknown"}
        if e.code in (401, 403):
            return {"status": "error", "detail": "VirusTotal rejected the API key"}
        if e.code == 429:
            return {"status": "error", "detail": "VirusTotal rate limit reached"}
        return {"status": "error", "detail": f"VirusTotal HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"status": "error", "detail": f"VirusTotal unreachable: {e}"}
    stats = data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
    return {"status": "found", "malicious": int(stats.get("malicious", 0)),
            "suspicious": int(stats.get("suspicious", 0)),
            "total": sum(int(v) for v in stats.values()),
            "link": f"https://www.virustotal.com/gui/file/{sha256}"}


def is_risky_to_open(report, name):
    return (ext_of(name) in DANGEROUS_EXTS
            or (report or {}).get("level") in ("caution", "danger"))
