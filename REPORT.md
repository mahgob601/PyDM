# PyDM test report — 116/116 passed

*2026-11-23 10:18:45*

## Environment

- **os**: Windows-2025Server-10.0.26100-SP0
- **python**: 3.12.10
- **cpu_count**: 4
- **PySide6**: 6.12.0
- **yt_dlp**: 2026.10.26
- **cryptography**: 50.0.2
- **psutil**: 7.2.2
- **ffmpeg**: ffmpeg version 9.1-essentials_build-www.gyan.dev Copyright (c) 2000-2026 the FFm
- **chrome**: C:\pydm-ci\chrome\chrome\win64-158.0.8127.36\chrome-win64\chrome.exe
- **defender**: {
    "AMServiceEnabled":  true,
    "RealTimeProtectionEnabled":  true,
    "AMProductVersion":  "4.18.26100.4"
}

## Results

### core — 14/14 (7s)
- ✅ multi-connection download intact + checksum verified
- ✅ wrong checksum flagged as danger
- ✅ exe disguised as .pdf flagged
- ✅ web page saved as .zip flagged
- ✅ double extension flagged
- ✅ 404 gives a clear error, no retry — `HTTP 404 · file not found`
- ✅ Content-Disposition: filename* (UTF-8) wins over ASCII fallback
- ✅ batch pattern expansion
- ✅ right-to-left trick removed and flagged
- ✅ new address for a different file is refused — `The new address points to a different file (6000000 bytes instead of 20000000)`
- ✅ new address for the same file resumes
- ✅ bridge: no-origin / website / DNS-rebinding requests rejected
- ✅ bridge: pairing approval then trusted
- ✅ bridge: denied extension stays blocked

### reliability — 8/8 (123s)
- ✅ safe archive extracted
- ✅ zip-slip archive refused, nothing escaped
- ✅ post-download command ran
- ✅ 7-Zip archive extracted
- ✅ streaming order downloads from the start — `8 pieces started`
- ✅ file changed while paused → restarted, new content
- ✅ server dies → retry scheduled, progress kept — `Network error: server refused the connection · kept 25 MB`
- ✅ server back → download finishes intact

### features — 21/21 (27s)
- ✅ FTP multi-connection with login — `7.0s, 8 parts`
- ✅ FTP wrong password → clear error — `FTP 530 Authentication failed.`
- ✅ FTP pause/resume — `paused at 46 MB`
- ✅ checksum found in SHA256SUMS and verified
- ✅ checksum found in .sha256 sidecar and verified
- ✅ checksum parser formats
- ✅ duplicate detected by hash
- ✅ history statistics
- ✅ page grabber: depth 2, same-site, de-duplicated — `movie720.mp4, manual.pdf, a.zip, b.zip, pic1.png, x.zip, deep.mp3, lvl3.iso`
- ✅ time-of-day speed rule
- ✅ yield to other apps
- ✅ psutil network counters work here (needed for 'yield')
- ✅ remote: page served with strict CSP
- ✅ remote: API requires the token
- ✅ remote: add from phone
- ✅ remote: lockout after repeated wrong tokens
- ✅ updates: valid signed manifest accepted
- ✅ updates: tampered manifest refused
- ✅ updates: installer verified against signed SHA-256
- ✅ phone notification (ntfy) sent
- ✅ bridge: batch + video endpoints

### hls — 9/9 (33s)
- ✅ HLS plain .ts — `Completed (30, ['audio', 'video720']) `
- ✅ HLS AES-128 explicit IV — `Completed (30, ['audio', 'video720']) `
- ✅ HLS AES-128 implicit IV — `Completed (30, ['audio', 'video720']) `
- ✅ HLS fMP4 — `Completed (30, ['audio', 'video720']) `
- ✅ HLS master → best + separate audio — `Completed (30, ['audio', 'video720']) `
- ✅ HLS master → 360p — `Completed (30, ['audio', 'video360']) `
- ✅ HLS DRM stream refused
- ✅ HLS decryption via ffmpeg (no cryptography pkg)
- ✅ video+audio job: pause → app restart → resume

### media — 6/6 (18s)
- ✅ yt-dlp: video on a web page
- ✅ yt-dlp: stream qualities listed — `['720p', '360p', 'Audio only', 'Audio only (MP3)']`
- ✅ separate video + audio merged (YouTube-style)
- ✅ MP3 conversion
- ✅ DASH stream via yt-dlp
- ✅ expired video link refreshed automatically

### windows — 17/17 (8s)
- ✅ Microsoft Defender command-line scanner found — `C:\ProgramData\Microsoft\Windows Defender\Platform\CurrentVersion\MpCmdRun.exe`
- ✅ Defender scan of a clean file → clean — `Microsoft Defender: no threats found`
- ✅ Mark-of-the-Web written (SmartScreen/Office protection) — `[ZoneTransfer]

ZoneId=3

HostUrl=http://127.0.0.1:53548/files/a.zip

`
- ✅ EICAR test virus caught (scan or real-time protection) — `Error · Blocked by your antivirus (Windows Security) — the file was flagged as dangerous and removed · file exists: True`
- ✅ blocked virus: clear antivirus message, never auto-retried — `Blocked by your antivirus (Windows Security) — the file was flagged as dangerous and removed`
- ✅ file pre-allocated to full size, plain NTFS (fast path) — `attributes=32, size=100000000, total=100000000, status=Downloading`
- ✅ keeps the PC awake while downloading (powercfg /requests) — `[PROCESS] \Device\HarddiskVolume3\Program Files\Python312\python.exe`
- ✅ full-screen detection API answers
- ✅ hostile file names download safely — `con='_CON.txt'/Completed · spaces='report.pdf.exe'/Completed · illegal='a_b_c_d_e_f_g_h.zip'/Completed · dots='trailing'/Completed · unicode='تقرير ✓ résumé.pdf'/Completed · rtlo='invoicefdp.exe'/Completed · long='xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.zip'/Completed`
- ✅ reserved name CON is renamed — `_CON.txt`
- ✅ no illegal characters / trailing dots — `'a_b_c_d_e_f_g_h.zip', 'trailing'`
- ✅ 'report.pdf      .exe' collapsed and flagged
- ✅ Arabic/accented names preserved — `تقرير ✓ résumé.pdf`
- ✅ RTLO name cleaned and flagged
- ✅ very long name shortened
- ✅ download folder with spaces + Unicode — `C:\pydm-ci\work\runs\windows\Téléchargements test ✓\Documents\manual.pdf`
- ✅ only one copy of PyDM can run — `False`

### extension — 23/23 (29s)
- ✅ extension: extension loads (MV3 service worker) — `mddphhpdoefjjfenjfminbephajioapa`
- ✅ extension: first request triggers pairing approval in PyDM — `chrome-extension://mddphhpdoefjjfenjfminbephajioapa`
- ✅ extension: catches /download?id=1 using the real file name — `report-2026.zip`
- ✅ extension: passes cookies + referrer + size — `sess=abc123 | http://127.0.0.1:53554/page.html | 600000`
- ✅ extension: browser's own download cancelled & erased — `[]`
- ✅ extension: small file (<256 KB) left to the browser — `small.zip`
- ✅ extension: 'never catch on this site' rule respected
- ✅ extension: detects page video + HLS playlist — `video:movie720.mp4, hls:index.m3u8`
- ✅ extension: toolbar badge shows video count — `2`
- ✅ extension: video button injected over the player — `Download video`
- ✅ extension: video button sends page + streams to PyDM — `movie720.mp4,index.m3u8`
- ✅ extension: button confirms 'Sent to PyDM' — `Sent to PyDM`
- ✅ extension: popup shows connection status — `Connected`
- ✅ extension: popup lists detected videos — `2 rows`
- ✅ extension: popup grabs page links (pages filtered out) — `Compressedtiny | Compressediso | Compressedp1 | Compressedp2 | Documentsguide | Imagespic | Videomovie720.mp4`
- ✅ extension: filter by type + send batch — `Download 4 files -> small.zip,ubuntu.iso,part1.rar,part2.rar`
- ✅ extension: popup saves settings — `{"minSizeKB":1024}`
- ✅ extension: context menu 'Download all links'
- ✅ extension: context menu 'Download with PyDM'
- ✅ extension: 'let the browser handle it for 5 min' works — `report-2026.zip,small.zip,ubuntu.iso`
- ✅ extension: web pages can't send downloads to PyDM — `blocked: Failed to fetch`
- ✅ extension: PyDM offline -> browser keeps the download — `report-2026.zip`
- ✅ extension: video button reports PyDM offline — `PyDM isn't running`

### gui — 9/9 (43s)
- ✅ app window, dialogs and settings render — `11 screenshots · `
- ✅ system tray available
- ✅ sidebar speed card intact on a 960x600 window — `spark 38px, card 157/157px`
- ✅ tray icon + notification work — `tray icon created`
- ✅ video dialog finds qualities — `3 options`
- ✅ grabber dialog lists files — `8 files`
- ✅ settings pages fit without sideways scrolling — `804x581`
- ✅ full-screen app detected (quiet mode trigger) — `before=False during=True`
- ✅ real app starts and keeps running (tray mode)

### speed — 4/4 (43s)
- ✅ speed: CDN, 1 MB/s per connection ≥ 12 MB/s — `14.4 MB/s in 6.6s incl. verification, transfer 15.6 MB/s, max, target 32`
- ✅ speed: flaky server ≥ 8 MB/s — `10.0 MB/s in 9.6s incl. verification, transfer 10.6 MB/s, max, target 32`
- ✅ speed: two mirrors ≥ 9 MB/s — `10.7 MB/s in 9.0s incl. verification, transfer 11.3 MB/s, optimal, target 8`
- ✅ speed: raw local (disk + engine, transfer) ≥ 80 MB/s — `81.3 MB/s in 4.7s incl. verification, transfer 137.3 MB/s, fixed, target 8`

### package — 5/5 (152s)
- ✅ PyInstaller builds PyDM.exe — `C:\pydm-ci\work\runs\pyinstaller\dist\PyDM\PyDM.exe`
- ✅ PyDM.exe starts and keeps running — `exit code None`
- ✅ PyDM.exe --cli downloads a file — `exit 0`
- ✅ PyDM.exe includes yt-dlp (video page download) — `exit 0`
- ✅ PyDM.exe includes cryptography (encrypted stream) — `exit 0`

## Metrics

- CDN, 1 MB/s per connection · transfer only: 15.6
- CDN, 1 MB/s per connection: 14.4
- flaky server · transfer only: 10.6
- flaky server: 10.0
- two mirrors · transfer only: 11.3
- two mirrors: 10.7
- raw local (disk + engine, transfer) · transfer only: 137.3
- raw local (disk + engine, transfer): 81.3
- bench server alone, 8 readers, no disk: 243.7
- disk alone, FileWriter, sparse file: 137.9
- disk alone, FileWriter, plain file: 1861.9
- exe folder MB: 132

## Notes

- windows · EICAR outcome: status=Error, error='Blocked by your antivirus (Windows Security) — the file was flagged as dangerous and removed', findings=[]
- speed · speed baseline: bench server alone: 243.7 MB/s; PyDM raw local: 81.3 MB/s
