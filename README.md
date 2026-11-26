# PyDM 4.4 — high-speed, secure download manager

A download manager in the spirit of Internet Download Manager. It's built for raw speed and adds what IDM lacks:

- downloading from several mirrors at once
- adaptive connection tuning
- a full browser extension with a video grabber
- a phone remote
- smart bandwidth sharing
- signed updates
- security checks on everything it downloads

## Install (Windows)
Download **PyDM-Setup-x.y.exe** from [Releases](https://github.com/mahgob601/PyDM/releases/latest) and run it.
- Installs for your account only, so no admin prompt (choose "all users" on the first page if you prefer).
- ffmpeg is included, so HD video and MP3 work out of the box.
- PyDM checks for updates on its own. Every update is signed, and the installer is verified before it runs.
- Windows may say "Windows protected your PC" because the installer isn't code-signed yet: click **More info → Run anyway**.

## Run from source
```
pip install -r requirements.txt      # PySide6 + optional extras (yt-dlp, cryptography, psutil, qrcode)
python run.py                        # or: python -m pydm   (--hidden = start in tray)
```
Install **ffmpeg** too (ffmpeg.org, or `winget install ffmpeg`) for HD video, MP3 and some streams.

Terminal mode:
```
python -m pydm --cli URL                         # auto-tuned connections
python -m pydm --cli URL -m MIRROR1 -m MIRROR2   # several servers at once
python -m pydm --cli https://youtu.be/… --video -q 1080
python -m pydm --cli https://…/master.m3u8 -n talk.mp4
python -m pydm --cli ftp://user:pass@host/file.iso -x --checksum sha256:<hash>
```
## Build the Windows installer
```
powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1     # PyDM.exe + ffmpeg + Inno Setup
powershell -ExecutionPolicy Bypass -File tools\smoke_installer.ps1   # install, run, self-update, uninstall
```
Output: `build\installer\PyDM-Setup-<version>.exe`.

## Publishing a release
1. Bump `VERSION` in `pydm/core/util.py`, commit, push.
2. GitHub → **Actions → release → Run workflow**, tick **publish** (or push a tag `v<version>`).

`.github/workflows/release.yml` then builds on GitHub's Windows machines, runs the installer smoke
test, signs `latest.json` with the `RELEASE_SIGNING_KEY` secret (it must match `PUBLIC_KEY` in
`pydm/release.py`), publishes the release and checks the live update feed. Leave **publish** unticked for a dry run.

## Speed: measured, v2 vs v3
Same 100 MB file and same machine, against a test server that simulates real-world conditions.

| Scenario | v2 | v3 |
|---|---|---|
| Server caps each connection at 1 MB/s (typical CDN) | 7.6 MB/s | **17.6 MB/s** (2.3×) |
| Flaky server — 25 % of connections stall at 150 KB/s | 5.1–5.4 MB/s | **12.9–15.3 MB/s** (~2.7×) |
| Same flaky server, both fixed at 8 connections | 5.1–5.4 MB/s | **7.1–10.2 MB/s** |
| Server rejects more than 12 connections | 7.6 MB/s (5.8 with 24 conns) | **10.0 MB/s** (learned the limit) |
| Two mirrors, each capped at 6 MB/s | 5.8 MB/s | **11.6 MB/s** (2×) |
| Raw throughput (no limits, localhost) | 380 MB/s | **540 MB/s** |

How v3 gets there:
- **Auto-tuned connections.** It starts with 8 and doubles every 1.5 s while throughput keeps rising, then settles. If the server pushes back (429/503), it backs off and remembers that server's limit for next time.
- **Keep-alive workers.** Each connection is reused for every piece it downloads, so there's no repeated TCP/TLS handshake.
- **Time-based work stealing.** A free connection takes over work from whichever piece will take *longest* to finish, not merely the largest one. A stuck connection hands most of its remaining bytes to a fast one.
- **Slow-connection replacement.** A connection running below 30 % of the median gets re-dialled, which often lands it on a faster server node.
- **Multi-source downloads.** The same file comes from several mirrors at once, with more connections going to the faster ones. Each mirror is checked for an identical size first.
- **Fast data path.** Data is read straight into preallocated buffers and written with positional writes (`pwrite`). Sockets use 4 MB receive buffers.
- **Windows file allocation.** Files are created as NTFS sparse files, so jumping ahead in a big file never stalls on zero-filling.
- **Connection budget.** A global limit (128 by default) keeps many simultaneous downloads from overwhelming your router.

## New in 4.4
- **Send to phone.** Any finished file gets a QR code. Scan it and the file downloads straight to
  your phone over Wi-Fi: no app, no cloud, no mobile data. The link is signed, works for 24 hours
  and covers only that file. The phone remote also has **Save to phone** buttons.

## New in 4.3
- **Instant preview.** Watch or listen while a file downloads. Jump anywhere and PyDM fetches that
  part first (about 0.4 s in tests, even when only 3 MB of 100 MB were downloaded). Plays in your
  browser, or in VLC for formats browsers can't play.
- **Send links from your phone, from anywhere.** In any app tap Share → ntfy → your private inbox
  topic, and your PC starts the download and confirms back. Video pages get the best quality up
  to 1080p. No port forwarding or Wi-Fi needed.
- **Download only part of a video.** "From 12:30 to 15:00" in the video window, or
  `--from 12:30 --to 15:00` on the command line. Only that part is downloaded. Exact cut is optional.
- **Tougher with bad servers.** Silent connections are replaced in ~2 s. If a server keeps going
  quiet with several connections, PyDM continues with one, like a browser.
- **Diagnostics.** A log of what servers answered (secrets redacted), plus Settings → Updates →
  Copy diagnostics.
- Settings shows whether the phone remote is running, and has an **Update now** button.

## New in v4
**Browser extension (Chrome, Edge, Brave; Firefox: rename `manifest.firefox.json` → `manifest.json`)**
- **Catching downloads:** it uses the browser's real file name, type and size, so links like `/download?id=123` are caught too. Cookies, referrer and size are passed along so logged-in downloads work, and Chrome's own copy is cancelled.
- **Your rules:** a minimum file size, which file types to catch, sites to never catch on, sites to always catch on, and "let the browser handle downloads for 5 minutes".
- **Video detection:** a counter on the toolbar icon, plus a "Download video" button over players on any page.
- **Popup:** connection status, the videos found on the page, and a grab-links tool with type filters and checkboxes.
- **Context menus:** download a link, a media file, the page's video, or all links.
- **Fallbacks and privacy:** if PyDM isn't running, the browser downloads normally. Downloads from private windows are never caught.

**Video downloader**
- **Sites:** YouTube and 1,800+ other sites through yt-dlp, with a quality picker (1080p, 720p, … audio only, MP3).
- **Speed:** HD video and audio are fetched in parallel with PyDM's own fast engine and merged with ffmpeg, without re-encoding.
- **HLS streams (.m3u8):** segments download in parallel over kept-alive connections. AES-128 encrypted streams, fMP4 and separate audio tracks are supported, and you can pick the quality.
- **DASH streams (.mpd):** handled through yt-dlp.
- **Expired links:** when a video link expires partway through, PyDM fetches a fresh one and continues.
- **Protected content:** DRM-protected and live streams are refused.

**Site grabber:** scans a page, and optionally pages it links to (up to 3 levels, same site only), for files. You can filter by type, extension or regex. Files you already have are unticked.

**Automatic checksum lookup:** finds `SHA256SUMS`, `file.sha256`, `.md5` and similar files on the same server and verifies against them automatically. A mismatch is flagged as a threat.

**Smart bandwidth**
- **Time rules:** e.g. 1 MB/s on weekdays 09:00–17:00. Rules can cross midnight.
- **Gives way to other apps:** slows down when another app (a call, streaming) needs the connection.
- **Quiet mode:** runs at reduced speed during full-screen games or presentations (Windows).

**Phone remote and notifications**
- **Remote control:** a web app protected by a secret link; scan its QR code from Settings. You can add, pause, resume and remove downloads.
- **Notifications:** push notifications through the free **ntfy** app when a download finishes, fails or is flagged as a threat.
- **Security:** a 256-bit access token, a lockout after repeated wrong tokens, a strict Content Security Policy, and no CORS.

**History and statistics:** today, the last week and all time. A daily chart, top sites, your fastest download and threats caught. History is searchable, and PyDM warns when you download a file you already have (matched by link or by its SHA-256 fingerprint).

**FTP / FTPS** with multiple connections and resume. Logins are given in the link.

**Signed auto-updates:** the update manifest must carry an Ed25519 signature, and the installer must match the signed SHA-256 before it's offered. `tools/sign_release.py` creates your key and signs releases. Put the URL and public key in `pydm/release.py`.

## Features from v3
- **Automatic retry.** Network drops, timeouts and 5xx errors are retried with growing waits (10 s → 5 min), and your progress is kept.
- **Streaming order.** Downloads from the start of the file first, so you can preview media early.
- **Safe auto-extract.** Handles zip and tar, plus 7z/rar if 7-Zip is installed. It blocks path traversal, symlinks and zip bombs, never extracts files flagged as threats, and applies Mark-of-the-Web to extracted files.
- **Post-download command.** Runs a command after each download with `{path} {folder} {name} {url}`, never through a shell.
- **Keeps the computer awake** while downloading (Windows, macOS and systemd Linux).
- **Integrity checks.** `If-Range` stops a file that changed on the server from being stitched together, and every server response is checked to match the exact bytes requested.
- **Expired links.** Paste a new address and PyDM continues the same download, after verifying it's the same file.
- Other features:
  - batch downloads with patterns (`file[001-150].jpg`)
  - a scheduler with time windows
  - checksums
  - per-download speed limits
  - proxies (including authentication)
  - custom headers
  - learned server limits you can reset
- **Disk-full handling.** A full disk is reported clearly and the download can be resumed after you free space.

## Security (from v2)
Download security:
- disguised-executable detection by magic bytes
- double-extension and hidden right-to-left-character detection
- HTTPS→HTTP redirect blocking
- TLS 1.2+ with certificate checking always on
- Microsoft Defender / ClamAV scanning
- optional VirusTotal hash lookup
- warnings before opening risky files

Browser and privacy protections:
- approval required for each browser extension before it can send downloads
- protection against DNS rebinding
- cookies and secret headers are never written to disk

## Browser integration (Chrome / Edge / Brave)
1. Open `chrome://extensions`, turn on Developer mode, click **Load unpacked** and choose the `browser-extension` folder in PyDM's install folder (Start menu → "PyDM browser extension (folder)"), or `extension` when running from source.
2. Right-click a link and choose **Download with PyDM**, or let it catch downloads automatically.
3. Approve the extension once when PyDM asks.

## How it was tested
- **Browser extension, real Chrome (23/23 passed):** catching, pairing, cookies, size and site rules, video detection, the video button, the popup and link grabber, context menus, the offline fallback, and blocking ordinary websites from talking to PyDM.
- **Features (37/37 passed):** FTP (multi-connection, logins, resume), checksum lookup, duplicates and history, the page grabber, bandwidth rules, remote control (login, lockout, security headers), signed updates (tampered manifest and wrong key both refused), ntfy, and the extension endpoints.
- **Video, checked with ffprobe:** plain, AES (explicit and implicit IV), fMP4 and master+audio HLS; DASH; YouTube-style separate video+audio merged; MP3; DRM refused; and pause → app restart → resume.
- **Engine:** mirrors, auto-tuning, slow-connection replacement, auto-retry while a server is down, If-Range change detection, and safe extraction that blocked a zip-slip attack.
