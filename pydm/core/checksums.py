"""Find checksums the website publishes next to a download and verify automatically.

Looks for (on the same server only):
  file.zip.sha256 / .sha256sum / .sha512 / .sha1 / .md5 / .DIGESTS
  SHA256SUMS, SHA256SUMS.txt, sha256sums.txt, SHA512SUMS, CHECKSUMS, checksums.txt, MD5SUMS
"""
import re
import threading
import urllib.parse
import urllib.request

ALGO_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
_HEX = re.compile(r"\b([0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|[0-9a-fA-F]{128})\b")
SUFFIXES = [".sha256", ".sha256sum", ".sha512", ".sha512sum", ".sha1", ".md5", ".DIGESTS"]
LISTS = ["SHA256SUMS", "SHA256SUMS.txt", "sha256sums.txt", "sha256sum.txt", "SHA512SUMS",
         "CHECKSUMS", "CHECKSUMS.txt", "checksums.txt", "MD5SUMS"]
MAX_BYTES = 512 * 1024
STRENGTH = {"sha512": 4, "sha256": 3, "sha1": 2, "md5": 1}


def candidates(url):
    p = urllib.parse.urlsplit(url)
    base = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    folder = base.rsplit("/", 1)[0] + "/"
    return [base + s for s in SUFFIXES] + [folder + name for name in LISTS]


def parse(text, filename):
    """Return {algo: hex} for `filename` from a checksum file's text."""
    found = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    target = filename.lower()
    for ln in lines:
        # "HASH  name", "HASH *name", "SHA256 (name) = HASH", "name: HASH"
        m = re.match(r"^(MD5|SHA1|SHA256|SHA512)\s*\((.+)\)\s*=\s*([0-9a-fA-F]+)$", ln, re.I)
        if m:
            if m.group(2).strip().lower().split("/")[-1] == target:
                h = m.group(3).lower()
                if ALGO_BY_LEN.get(len(h)):
                    found[ALGO_BY_LEN[len(h)]] = h
            continue
        hm = _HEX.search(ln)
        if not hm:
            continue
        rest = (ln[:hm.start()] + ln[hm.end():]).strip(" \t*:").lower()
        name = rest.split("/")[-1].split("\\")[-1].strip()
        if name == target:
            found[ALGO_BY_LEN[len(hm.group(1))]] = hm.group(1).lower()
    if not found:
        # a sidecar file containing only a bare hash applies to its own file
        m = re.fullmatch(r"\s*([0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|"
                         r"[0-9a-fA-F]{128})\s*", text)
        if m:
            found[ALGO_BY_LEN[len(m.group(1))]] = m.group(1).lower()
    return found


def _fetch(url, opener, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            ctype = (r.headers.get_content_type() or "").lower()
            if ctype.startswith(("text/html", "image/", "video/", "audio/")):
                return None
            data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None
            return data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def lookup(url, filename, opener, headers=None, timeout=8):
    """Find the strongest published checksum for `filename`. Returns (algo, hex, source) or None."""
    urls = candidates(url)
    results = {}
    sidecar_names = {url_.rsplit("/", 1)[-1] for url_ in urls[:len(SUFFIXES)]}

    def worker(u):
        text = _fetch(u, opener, headers or {}, timeout)
        if text:
            name = u.rsplit("/", 1)[-1]
            parsed = parse(text, filename)
            if not parsed and name in sidecar_names:
                parsed = {}
            results[u] = parsed
    threads = [threading.Thread(target=worker, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 2)
    best = None
    for u in urls:                      # keep the deterministic candidate order
        for algo, h in (results.get(u) or {}).items():
            if best is None or STRENGTH[algo] > STRENGTH[best[0]]:
                best = (algo, h, u)
    return best
