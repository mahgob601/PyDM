"""HLS (.m3u8) downloader.

Segments are fetched in parallel over kept-alive connections (one per worker), written
to a work folder, decrypted (AES-128) when needed, then joined in order and remuxed to MP4
with ffmpeg when it is available. DRM-protected (SAMPLE-AES / Widevine / FairPlay) streams
are refused.
"""
import os
import re
import shutil
import threading
import time
import urllib.parse

from . import media

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


class HlsError(Exception):
    pass


def parse_attrs(text):
    return {k: v.strip('"') for k, v in _ATTR.findall(text)}


def parse_playlist(text, base_url):
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise HlsError("Not an HLS playlist")
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        variants, audio = [], []
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF:"):
                a = parse_attrs(ln.split(":", 1)[1])
                uri = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
                if not uri:
                    continue
                res = a.get("RESOLUTION", "")
                height = int(res.split("x")[1]) if "x" in res else None
                variants.append({"url": urllib.parse.urljoin(base_url, uri),
                                 "bandwidth": int(a.get("BANDWIDTH", "0") or 0),
                                 "height": height, "codecs": a.get("CODECS", ""),
                                 "audio": a.get("AUDIO")})
            elif ln.startswith("#EXT-X-MEDIA:"):
                a = parse_attrs(ln.split(":", 1)[1])
                if a.get("TYPE") == "AUDIO" and a.get("URI"):
                    audio.append({"group": a.get("GROUP-ID"), "name": a.get("NAME", ""),
                                  "language": a.get("LANGUAGE", ""),
                                  "default": a.get("DEFAULT") == "YES",
                                  "url": urllib.parse.urljoin(base_url, a["URI"])})
        variants.sort(key=lambda v: (v["height"] or 0, v["bandwidth"]), reverse=True)
        return {"type": "master", "variants": variants, "audio": audio}

    segs, key, mp, seq = [], None, None, 0
    drm, ended, duration, byterange, last_end = False, False, None, None, {}
    for ln in lines:
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(ln.split(":", 1)[1] or 0)
        elif ln.startswith("#EXT-X-KEY:") or ln.startswith("#EXT-X-SESSION-KEY:"):
            a = parse_attrs(ln.split(":", 1)[1])
            method = a.get("METHOD", "NONE")
            keyformat = a.get("KEYFORMAT", "identity")
            if method == "NONE":
                key = None
            elif method == "AES-128" and keyformat == "identity":
                key = {"uri": urllib.parse.urljoin(base_url, a.get("URI", "")),
                       "iv": a.get("IV")}
            else:
                drm = True
        elif ln.startswith("#EXT-X-MAP:"):
            a = parse_attrs(ln.split(":", 1)[1])
            rng = None
            if a.get("BYTERANGE"):
                n, _, o = a["BYTERANGE"].partition("@")
                rng = (int(o or 0), int(o or 0) + int(n) - 1)
            mp = {"url": urllib.parse.urljoin(base_url, a["URI"]), "range": rng}
        elif ln.startswith("#EXTINF:"):
            duration = float(ln.split(":", 1)[1].split(",")[0] or 0)
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            n, _, o = ln.split(":", 1)[1].partition("@")
            byterange = (int(n), int(o) if o else None)
        elif ln.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif ln and not ln.startswith("#"):
            url = urllib.parse.urljoin(base_url, ln)
            rng = None
            if byterange:
                n, o = byterange
                o = last_end.get(url, 0) if o is None else o
                rng = (o, o + n - 1)
                last_end[url] = o + n
                byterange = None
            segs.append({"url": url, "duration": duration or 0, "range": rng,
                         "key": dict(key) if key else None, "seq": seq + len(segs)})
            duration = None
    return {"type": "media", "segments": segs, "map": mp, "drm": drm, "ended": ended,
            "duration": sum(s["duration"] for s in segs)}


def fetch_text(d, url):
    from .engine import HttpConn
    conn = HttpConn(d)
    try:
        r, final = conn.fetch(url)
        if r.status >= 400:
            raise HlsError(f"Playlist request failed (HTTP {r.status})")
        data = r.read(16 * 1024 * 1024)
        return data.decode("utf-8", "replace"), final
    finally:
        conn.close()


def _decryptor():
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return None

    def decrypt(data, key, iv):
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        out = dec.update(data) + dec.finalize()
        unpad = padding.PKCS7(128).unpadder()
        try:
            return unpad.update(out) + unpad.finalize()
        except ValueError:
            return out
    return decrypt


def build_state(d):
    """Resolve the playlist into a persisted segment list on d.hls."""
    text, final = fetch_text(d, d.url)
    pl = parse_playlist(text, final)
    if pl["type"] == "master":
        if not pl["variants"]:
            raise HlsError("Playlist has no streams")
        want = (d.media or {}).get("height")
        pick = pl["variants"][0]
        if want:
            fits = [v for v in pl["variants"] if (v["height"] or 0) <= want]
            pick = fits[0] if fits else pl["variants"][-1]
        text, final = fetch_text(d, pick["url"])
        pl = parse_playlist(text, final)
    if pl["drm"]:
        from .engine import BlockedError
        raise BlockedError("This stream is DRM-protected — PyDM doesn't download protected "
                           "content")
    if not pl["ended"]:
        from .engine import BlockedError
        raise BlockedError("This is a live stream — only finished videos can be downloaded")
    if not pl["segments"]:
        raise HlsError("Playlist has no segments")
    keys, segs = [], []
    for s in pl["segments"]:
        ki = None
        if s["key"]:
            k = (s["key"]["uri"], s["key"]["iv"])
            if k not in keys:
                keys.append(k)
            ki = keys.index(k)
        r = s["range"]
        segs.append([s["url"], r[0] if r else None, r[1] if r else None, ki, s["seq"], 0])
    mp = pl["map"]
    d.hls = {"playlist": final, "segments": segs, "keys": [list(k) for k in keys],
             "map": [mp["url"], *(mp["range"] or (None, None))] if mp else None,
             "duration": pl["duration"]}


def _key_bytes(d, conn, uri, cache):
    if uri not in cache:
        r, _ = conn.fetch(uri)
        if r.status >= 400:
            raise HlsError(f"Couldn't get the decryption key (HTTP {r.status})")
        data = r.read(64)
        if len(data) != 16:
            raise HlsError("Invalid AES-128 key")
        cache[uri] = data
    return cache[uri]


def run(d, finalize_container=True):
    """Download d (kind 'hls'). Sets d.temp_path content; returns path of the joined file."""
    from .engine import HttpConn, Source, Worker, IncompleteError
    if not d.hls or not d.hls.get("segments"):
        build_state(d)
    st = d.hls
    segs = st["segments"]
    work = d.path + ".pydm-hls"
    os.makedirs(work, exist_ok=True)
    decrypt = _decryptor()
    encrypted = bool(st["keys"])
    use_ffmpeg_crypto = encrypted and decrypt is None
    if use_ffmpeg_crypto and not media.ffmpeg_path(d._settings()):
        from .engine import BlockedError
        raise BlockedError("This stream is encrypted — install ffmpeg (or the 'cryptography' "
                           "package) to save it")
    d.hls_total = len(segs) + (1 if st.get("map") else 0)
    host_src = Source(st["playlist"], st["playlist"], None, primary=True)
    d.sources = [host_src]
    lock = threading.Lock()
    queue_idx = [i for i, s in enumerate(segs) if not s[5]]
    if st.get("map") and not os.path.exists(os.path.join(work, "init.mp4")):
        queue_idx.insert(0, -1)
    key_cache, errors = {}, []

    def count_done():
        n = sum(1 for s in segs if s[5])
        return n + (1 if st.get("map") and os.path.exists(os.path.join(work, "init.mp4")) else 0)
    d.hls_done = count_done()

    def worker(w):
        conn = HttpConn(d)
        try:
            while not d._halted():
                with lock:
                    if not queue_idx:
                        return
                    i = queue_idx.pop(0)
                if i == -1:
                    url, a, b = st["map"]
                    target, seg = os.path.join(work, "init.mp4"), None
                else:
                    seg = segs[i]
                    url, a, b = seg[0], seg[1], seg[2]
                    target = os.path.join(work, f"{i:06d}.seg")
                for attempt in range(6):
                    try:
                        extra = {"Range": f"bytes={a}-{b}"} if a is not None else None
                        r, _ = conn.fetch(url, extra)
                        if r.status >= 400:
                            from .engine import HTTPStatusError
                            raise HTTPStatusError(r.status, r.reason)
                        chunks = []
                        while True:
                            n = r.readinto(w.mv)
                            if not n:
                                break
                            d._consume(n)
                            chunks.append(bytes(w.mv[:n]))
                            with d.lock:
                                d._window += n
                                w.window += n
                                host_src.bytes += n
                                d.bytes_done += n
                            if d._halted():
                                conn.close()
                                with lock:
                                    queue_idx.insert(0, i)
                                return
                        data = b"".join(chunks)
                        if seg is not None and seg[3] is not None and decrypt:
                            kuri, iv = st["keys"][seg[3]]
                            key = _key_bytes(d, conn, kuri, key_cache)
                            ivb = (bytes.fromhex(iv[2:] if iv.lower().startswith("0x") else iv)
                                   if iv else seg[4].to_bytes(16, "big"))
                            data = decrypt(data, key, ivb.rjust(16, b"\0"))
                        with open(target + ".tmp", "wb") as f:
                            f.write(data)
                        os.replace(target + ".tmp", target)
                        if seg is not None:
                            seg[5] = 1
                        with lock:
                            d.hls_done += 1
                        break
                    except Exception as e:  # noqa: BLE001
                        conn.close()
                        if d._halted():
                            with lock:
                                queue_idx.insert(0, i)
                            return
                        code = getattr(e, "code", None)
                        if (code and 400 <= code < 500 and code != 429) or attempt == 5:
                            errors.append(e)
                            d._abort = d._abort or f"Segment failed: {e}"
                            return
                        d.stop_event.wait(min(2 ** attempt * 0.5, 10))
        finally:
            conn.close()
            with d.lock:
                w.alive = False
                w.source.active -= 1

    n = max(1, min(d.connections or 8, 16, len(queue_idx) or 1))
    workers = []
    for _ in range(n):
        w = Worker(host_src)
        host_src.active += 1
        workers.append(w)
        threading.Thread(target=worker, args=(w,), daemon=True).start()
    with d.lock:
        d.workers = workers
    last_save = time.monotonic()
    while any(w.alive for w in workers):
        d._tick_speed()
        if time.monotonic() - last_save > 3:
            d._save()
            last_save = time.monotonic()
        time.sleep(0.2)
    if d._abort or d.stop_event.is_set():
        return None
    if count_done() < d.hls_total:
        raise IncompleteError("Some video segments are missing")
    return join(d, work, use_ffmpeg_crypto, finalize_container)


def _ff_name(work, i, ext):
    src, name = os.path.join(work, f"{i:06d}.seg"), f"{i:06d}{ext}"
    dst = os.path.join(work, name)
    if not os.path.exists(dst):
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
    return name


def join(d, work, use_ffmpeg_crypto, finalize_container):
    st = d.hls
    segs = st["segments"]
    out = d.temp_path
    ff = media.ffmpeg_path(d._settings())
    if use_ffmpeg_crypto:
        local = os.path.join(work, "local.m3u8")
        # ffmpeg >= 7 refuses HLS segments whose extension it doesn't know (".seg"), so the
        # playlist points at hard links with standard names; no data is copied.
        seg_ext = ".m4s" if st.get("map") else ".ts"
        with open(local, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:30\n")
            if st.get("map"):
                f.write('#EXT-X-MAP:URI="init.mp4"\n')
            from .engine import HttpConn
            conn = HttpConn(d)
            cache = {}
            try:
                last = None
                for i, s in enumerate(segs):
                    if s[3] is not None and s[3] != last:
                        kuri, iv = st["keys"][s[3]]
                        kpath = os.path.join(work, f"key{s[3]}.bin")
                        with open(kpath, "wb") as kf:
                            kf.write(_key_bytes(d, conn, kuri, cache))
                        ivs = f",IV={iv}" if iv else ""
                        f.write(f'#EXT-X-KEY:METHOD=AES-128,URI="key{s[3]}.bin"{ivs}\n')
                        last = s[3]
                    f.write(f"#EXT-X-MEDIA-SEQUENCE:{s[4]}\n" if i == 0 else "")
                    f.write(f"#EXTINF:1.0,\n{_ff_name(work, i, seg_ext)}\n")
            finally:
                conn.close()
            f.write("#EXT-X-ENDLIST\n")
        tmp_out = out + ".ts"
        media.run_ffmpeg(ff, ["-allowed_extensions", "ALL", "-protocol_whitelist",
                              "file,crypto,data", "-i", local, "-c", "copy", "-f", "mpegts",
                              tmp_out])
        os.replace(tmp_out, out)
    else:
        with open(out, "wb") as f:
            if st.get("map"):
                with open(os.path.join(work, "init.mp4"), "rb") as src:
                    shutil.copyfileobj(src, f)
            for i in range(len(segs)):
                with open(os.path.join(work, f"{i:06d}.seg"), "rb") as src:
                    shutil.copyfileobj(src, f, 4 * 1024 * 1024)
    shutil.rmtree(work, ignore_errors=True)
    d.total = os.path.getsize(out)
    if finalize_container and ff and not st.get("map"):
        # MPEG-TS → MP4 (no re-encoding) so every player handles it
        mp4 = out + ".mp4"
        try:
            media.run_ffmpeg(ff, ["-i", out, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                                  "-movflags", "+faststart", mp4])
            os.replace(mp4, out)
            if not d.filename.lower().endswith((".mp4", ".m4a")):
                d.retarget_extension(".mp4")
        except Exception:  # noqa: BLE001 — keep the .ts if remuxing fails
            if os.path.exists(mp4):
                os.remove(mp4)
            if not d.filename.lower().endswith(".ts"):
                d.retarget_extension(".ts")
    elif not st.get("map") and finalize_container and not d.filename.lower().endswith(".ts"):
        d.retarget_extension(".ts")
    return out
