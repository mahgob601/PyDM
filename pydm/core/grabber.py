"""Site grabber: collect downloadable links from a page (optionally following same-site
pages a few levels deep)."""
import collections
import html.parser
import re
import urllib.parse
import urllib.request

from .util import CATEGORIES, category_for, ext_of, is_http_url

PAGE_EXTS = {"", "html", "htm", "php", "asp", "aspx", "jsp", "shtml", "xhtml", "cgi"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "tif", "tiff"}
MAX_PAGE_BYTES = 5 * 1024 * 1024
TYPE_GROUPS = dict(CATEGORIES, Images=sorted(IMAGE_EXTS))


class _LinkParser(html.parser.HTMLParser):
    ATTRS = {"a": "href", "img": "src", "video": "src", "audio": "src", "source": "src",
             "track": "src", "embed": "src", "iframe": "src", "link": "href"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []          # (url, text, tag)
        self.base = None
        self._a = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "base" and a.get("href"):
            self.base = a["href"]
        attr = self.ATTRS.get(tag)
        if attr and a.get(attr):
            if tag == "a":
                self._a = a[attr]
                self._text = []
            else:
                self.links.append((a[attr], a.get("alt") or a.get("title") or "", tag))
        if tag == "img" and a.get("srcset"):
            for part in a["srcset"].split(","):
                u = part.strip().split(" ")[0]
                if u:
                    self.links.append((u, a.get("alt") or "", "img"))

    def handle_data(self, data):
        if self._a is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append((self._a, " ".join("".join(self._text).split())[:200], "a"))
            self._a = None


def parse_links(html_text, page_url):
    p = _LinkParser()
    try:
        p.feed(html_text)
    except Exception:  # noqa: BLE001 — broken HTML: keep what we have
        pass
    base = urllib.parse.urljoin(page_url, p.base) if p.base else page_url
    out = []
    for raw, text, tag in p.links:
        raw = raw.strip()
        if raw.startswith(("javascript:", "mailto:", "data:", "#", "tel:")):
            continue
        u = urllib.parse.urldefrag(urllib.parse.urljoin(base, raw))[0]
        if is_http_url(u) or u.startswith(("ftp://", "ftps://")):
            out.append((u, text, tag))
    return out


def classify(url):
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    ext = ext_of(name)
    if ext in IMAGE_EXTS:
        return "Images"
    cat = category_for(name)
    return cat if cat != "Other" or ext not in PAGE_EXTS else ("Page" if ext in PAGE_EXTS
                                                              else "Other")


def fetch_page(url, opener, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype:
            return None, r.geturl()
        data = r.read(MAX_PAGE_BYTES)
        head = data[:2048].lstrip().lower()
        if not ctype or "text/plain" in ctype:
            if not (head.startswith((b"<!doctype html", b"<html")) or b"<body" in head
                    or b"<a " in head):
                return None, r.geturl()
        charset = r.headers.get_content_charset() or "utf-8"
        return data.decode(charset, "replace"), r.geturl()


def grab(url, opener, headers=None, depth=0, max_pages=60, same_site=True, progress=None,
         stop=None):
    """Return ordered list of dicts: url, text, type, page. Pages are followed up to `depth`."""
    headers = dict(headers or {})
    root_host = urllib.parse.urlsplit(url).hostname
    seen_pages, found, order = set(), {}, []
    queue = collections.deque([(url, 0)])
    pages = 0
    while queue and pages < max_pages:
        if stop is not None and stop.is_set():
            break
        page, level = queue.popleft()
        if page in seen_pages:
            continue
        seen_pages.add(page)
        try:
            text, final = fetch_page(page, opener, {**headers, "Referer": page})
        except Exception:  # noqa: BLE001
            continue
        pages += 1
        if progress:
            progress(pages, len(found), page)
        if text is None:
            continue
        for u, label, tag in parse_links(text, final):
            kind = classify(u)
            if kind == "Page" and tag in ("a", "iframe"):
                if level < depth and (not same_site
                                      or urllib.parse.urlsplit(u).hostname == root_host):
                    queue.append((u, level + 1))
                continue
            if kind == "Page" or tag == "link" and kind not in TYPE_GROUPS:
                continue
            if u not in found:
                found[u] = {"url": u, "text": label, "type": kind, "page": final}
                order.append(u)
    return [found[u] for u in order]


def matches(item, types, ext_text="", pattern=""):
    if types and item["type"] not in types:
        extra = {e.strip(". ").lower() for e in re.split(r"[,\s]+", ext_text) if e.strip()}
        name = urllib.parse.urlsplit(item["url"]).path
        if not extra or ext_of(name) not in extra:
            return False
    if pattern:
        try:
            return re.search(pattern, item["url"] + " " + item["text"], re.I) is not None
        except re.error:
            return pattern.lower() in (item["url"] + " " + item["text"]).lower()
    return True
