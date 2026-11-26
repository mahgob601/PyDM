"""Video support: ffmpeg helpers and yt-dlp extraction (quality options)."""
import os
import re
import shutil
import subprocess
import sys

from .util import safe_filename

VIDEO_EXT_PREF = {"mp4": 0, "webm": 1}
AUDIO_FOR = {"mp4": ("m4a", "mp4"), "webm": ("webm", "opus")}


class MediaError(Exception):
    pass


def ffmpeg_path(settings=None):
    custom = (settings or {}).get("ffmpeg_path")
    if custom and os.path.isfile(custom):
        return custom
    app_dir = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                              else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for cand in (os.path.join(app_dir, "ffmpeg", "ffmpeg.exe"),
                 os.path.join(app_dir, "ffmpeg", "ffmpeg")):
        if os.path.isfile(cand):                 # shipped with the Windows installer
            return cand
    p = shutil.which("ffmpeg")
    if p:
        return p
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.path.expanduser(r"~\scoop\shims"), r"C:\ffmpeg\bin"):
            for cand in (os.path.join(base, "ffmpeg", "bin", "ffmpeg.exe"),
                         os.path.join(base, "ffmpeg.exe")):
                if os.path.isfile(cand):
                    return cand
    return None


def run_ffmpeg(ff, args, timeout=6 * 3600):
    if not ff:
        raise MediaError("ffmpeg is required for this step")
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise MediaError("ffmpeg failed: " + r.stderr.decode("utf-8", "replace")[-300:])


def ytdlp_available():
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def js_runtime():
    """JavaScript runtime for yt-dlp (YouTube needs one): bundled Deno first, then PATH."""
    exe = "deno.exe" if os.name == "nt" else "deno"
    bundled = os.path.join(_app_dir(), "deno", exe)
    if os.path.isfile(bundled):
        return "deno", bundled
    for name in ("deno", "node", "bun"):
        p = shutil.which(name)
        if p:
            return name, p
    return None, None


class _Log:
    """Keep yt-dlp's warnings: when a site gives no formats, they say why."""
    def __init__(self):
        self.warnings = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).replace("WARNING: ", ""))

    def error(self, msg):
        self.warning(msg)


def _ydl_opts(headers=None, cookies=None, log=None):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20, "extract_flat": False,
            # list what exists; PyDM picks the format itself (avoids "Requested format is
            # not available" when a site only offers unusual formats)
            "ignore_no_formats_error": True}
    if log is not None:
        opts["logger"] = log
        opts["no_warnings"] = False
    name, path = js_runtime()
    if name:
        opts["js_runtimes"] = {name: {"path": path}}
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = cookies
    if h:
        opts["http_headers"] = h
    return opts


def resolve(page_url, headers=None, cookies=None):
    """Run yt-dlp extraction. Returns a normalized info dict."""
    try:
        import yt_dlp
    except ImportError as e:
        raise MediaError("Install yt-dlp to download from video websites "
                         "(pip install yt-dlp)") from e
    log = _Log()
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(headers, cookies, log)) as ydl:
            info = ydl.extract_info(page_url, download=False)
    except Exception as e:  # noqa: BLE001
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        raise MediaError(msg[:300]) from e
    if info.get("_type") == "playlist":
        entries = [x for x in info.get("entries") or [] if x]
        if not entries:
            raise MediaError("No videos found on this page")
        info = entries[0]
    if info.get("is_live"):
        raise MediaError("This is a live stream — only finished videos can be downloaded")
    formats = []
    for f in info.get("formats") or [info]:
        if f.get("has_drm"):
            continue
        proto = f.get("protocol") or ""
        if not f.get("url"):
            continue
        vcodec, acodec = f.get("vcodec") or "none", f.get("acodec") or "none"
        if vcodec == "none" and acodec == "none":
            # codecs unknown (common for HLS renditions): use yt-dlp's other hints
            if f.get("resolution") == "audio only" or (
                    not f.get("height") and (f.get("audio_ext") or "none") != "none"
                    and (f.get("video_ext") or "none") == "none"):
                acodec = "unknown"
            elif f.get("height"):
                vcodec = "unknown"
                if not f.get("format_id", "").startswith(("video", "v")):
                    acodec = "none"
        formats.append({
            "id": f.get("format_id"), "ext": f.get("ext"), "height": f.get("height"),
            "fps": f.get("fps"), "vcodec": vcodec,
            "acodec": acodec, "tbr": f.get("tbr") or 0,
            "size": f.get("filesize") or f.get("filesize_approx"),
            "url": f["url"], "protocol": proto, "headers": f.get("http_headers") or {},
            "note": f.get("format_note") or ""})
    if not formats:
        if "youtube" in (info.get("extractor_key") or info.get("extractor") or "").lower() \
                and js_runtime()[0] is None:
            raise MediaError("YouTube needs a JavaScript runtime that's missing: reinstall "
                             "PyDM (it ships one), or install Deno from deno.com")
        if info.get("formats") and all(f.get("has_drm") for f in info["formats"]):
            raise MediaError("This video is DRM-protected and can't be downloaded")
        why = next((w for w in log.warnings if "format" in w.lower() or "sign in" in w.lower()
                    or "challenge" in w.lower() or "bot" in w.lower()),
                   log.warnings[0] if log.warnings else "")
        raise MediaError("No downloadable formats found" + (f" — {why[:240]}" if why else
                         " — the site may need you to sign in (open the page in Chrome, "
                         "then use the PyDM button there)"))
    return {"title": info.get("title") or "video", "page_url": info.get("webpage_url") or page_url,
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "extractor": info.get("extractor_key") or "", "formats": formats}


def engine_for(fmt):
    proto = fmt["protocol"]
    if proto in ("https", "http", ""):
        return "file"
    if proto.startswith("m3u8"):
        return "hls"
    return "ytdlp"


def _has_video(f):
    return f["vcodec"] != "none"


def _has_audio(f):
    return f["acodec"] != "none"


def choices(info, have_ffmpeg=True):
    """Build user-facing quality options, best first.

    Each option: label, kind ('single' | 'merge' | 'audio'), video, audio, container, size."""
    fmts = info["formats"]
    progressive = [f for f in fmts if _has_video(f) and _has_audio(f)]
    video_only = [f for f in fmts if _has_video(f) and not _has_audio(f)]
    audio_only = [f for f in fmts if _has_audio(f) and not _has_video(f)]
    unknown = [f for f in fmts if f["vcodec"] == "none" and f["acodec"] == "none"
               and f["ext"] in ("mp4", "webm", "mkv", "mov", "m3u8", "flv")]
    progressive += unknown            # generic pages often don't report codecs

    def best_audio(container):
        prefs = AUDIO_FOR.get(container, ("m4a",))
        cands = [a for a in audio_only if a["ext"] in prefs] or audio_only
        return max(cands, key=lambda a: a["tbr"] or 0) if cands else None

    opts, seen = [], set()
    heights = sorted({f["height"] for f in video_only + progressive if f["height"]},
                     reverse=True)
    for h in heights:
        cands = []
        if have_ffmpeg:
            for v in video_only:
                if v["height"] == h:
                    a = best_audio(v["ext"] if v["ext"] in AUDIO_FOR else "mp4")
                    if a:
                        cont = "mp4" if v["ext"] == "mp4" and a["ext"] in ("m4a", "mp4") \
                            else ("webm" if v["ext"] == "webm" and a["ext"] == "webm" else "mkv")
                        cands.append(("merge", v, a, cont))
        for p in progressive:
            if p["height"] == h:
                cands.append(("single", p, None, p["ext"] if p["ext"] != "m3u8" else "mp4"))
        if not cands:
            continue
        # prefer MP4, then higher bitrate
        cands.sort(key=lambda c: (VIDEO_EXT_PREF.get(c[3], 2),
                                  -((c[1]["tbr"] or 0) + ((c[2] or {}).get("tbr") or 0))))
        kind, v, a, cont = cands[0]
        if h in seen:
            continue
        seen.add(h)
        size = (v.get("size") or 0) + ((a or {}).get("size") or 0) or None
        fps = f"{int(v['fps'])}fps" if v.get("fps") and v["fps"] > 30 else ""
        opts.append({"label": f"{h}p{fps}", "kind": kind, "video": v, "audio": a,
                     "container": cont, "size": size, "height": h})
    for p in progressive:
        if not p["height"] and not any(o["video"] is p for o in opts):
            opts.append({"label": f"{p['ext'].upper()} {p['note']}".strip(), "kind": "single",
                         "video": p, "audio": None,
                         "container": p["ext"] if p["ext"] != "m3u8" else "mp4",
                         "size": p.get("size"), "height": None})
    if audio_only:
        a = max(audio_only, key=lambda x: x["tbr"] or 0)
        opts.append({"label": "Audio only", "kind": "audio", "video": None, "audio": a,
                     "container": "m4a" if a["ext"] in ("m4a", "mp4") else a["ext"],
                     "size": a.get("size"), "height": None})
        if have_ffmpeg:
            opts.append({"label": "Audio only (MP3)", "kind": "mp3", "video": None, "audio": a,
                         "container": "mp3", "size": a.get("size"), "height": None})
    return opts


def title_filename(title, container):
    return safe_filename(f"{title[:150]}.{container}")


def find_format(info, fmt_id):
    return next((f for f in info["formats"] if f["id"] == fmt_id), None)


def parse_time(text):
    """'90', '1:30', '01:02:03', '1h2m3s', '2m' → seconds (float). None if empty."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t)
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}(\.\d+)?", t):
        secs = 0.0
        for part in t.split(":"):
            secs = secs * 60 + float(part)
        return secs
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", t)
    if m and any(m.groups()):
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    raise MediaError(f"Couldn't read the time “{text}” — use 1:30, 01:02:03 or 90")


def fmt_clock(secs):
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def check_clip(start, end, duration=None):
    if start is None or end is None:
        raise MediaError("Enter both a start and an end time")
    if end <= start:
        raise MediaError("The end time must be after the start time")
    if duration and start >= duration:
        raise MediaError(f"The video is only {fmt_clock(duration)} long")
    return start, min(end, duration) if duration else end


def run_ytdlp(d):
    """Let yt-dlp download formats our engine can't fetch directly (DASH fragments, etc.).
    Returns the final file path, or None when paused."""
    try:
        import yt_dlp
    except ImportError as e:
        raise MediaError("Install yt-dlp to download this video (pip install yt-dlp)") from e
    m = d.media or {}
    ff = ffmpeg_path(d._settings())
    base = os.path.splitext(d.filename)[0]
    out_dir = d.save_dir
    cont = m.get("container") or "mp4"

    def hook(st):
        if d.stop_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("paused")
        done = st.get("downloaded_bytes") or 0
        total = st.get("total_bytes") or st.get("total_bytes_estimate")
        with d.lock:
            got = max(0, done - getattr(d, "_ytdl_last", 0))
            d._window += got
            d._ytdl_last = done
        d._count(got)
        d.bytes_done = done
        if total:
            d.total = int(total)
        d._tick_speed()
        if st.get("status") == "finished":
            d._ytdl_last = 0

    opts = {"format": m.get("format") or "bv*+ba/b", "outtmpl": os.path.join(out_dir, base +
                                                                            ".%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "continuedl": True, "progress_hooks": [hook], "socket_timeout": 30,
            "concurrent_fragment_downloads": max(1, min(16, d.connections or 8)),
            "retries": 10, "fragment_retries": 10}
    if ff:
        opts["ffmpeg_location"] = ff
    name, path = js_runtime()
    if name:
        opts["js_runtimes"] = {name: {"path": path}}
    clip = m.get("clip")
    if clip:                                   # only this part: fetched, not trimmed later
        if not ff:
            raise MediaError("Downloading part of a video needs ffmpeg")
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [tuple(clip)])
        opts["force_keyframes_at_cuts"] = bool(m.get("exact"))
    if cont in ("mp4", "mkv", "webm") and m.get("op") != "mp3":
        opts["merge_output_format"] = cont
    if m.get("op") == "mp3":
        if not ff:
            raise MediaError("Install ffmpeg to convert to MP3")
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "0"}]
    headers = {}
    if d.cookies:
        headers["Cookie"] = d.cookies
    if d.referrer:
        headers["Referer"] = d.referrer
    if headers:
        opts["http_headers"] = headers
    d._ytdl_last = 0
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(m.get("page_url") or d.url, download=True)
            if info.get("_type") == "playlist":
                info = (info.get("entries") or [None])[0] or {}
            req = info.get("requested_downloads") or []
            path = req[0].get("filepath") if req else ydl.prepare_filename(info)
            if m.get("op") == "mp3":
                path = os.path.splitext(path)[0] + ".mp3"
    except yt_dlp.utils.DownloadCancelled:
        return None
    except Exception as e:  # noqa: BLE001
        if d.stop_event.is_set():
            return None
        raise MediaError(re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:300]) \
            from e
    if not path or not os.path.exists(path):
        raise MediaError("yt-dlp finished but the file wasn't found")
    return path
