"""Creates every test file, video and stream used by the suites (any OS; needs ffmpeg)."""
import glob
import hashlib
import os
import shutil
import subprocess
import zipfile


def _rand(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        left = size
        while left:
            n = min(left, 8 * 1024 * 1024)
            f.write(os.urandom(n))
            left -= n


def _ff(ff, *args):
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def build(W, ff, log=print):
    marker = os.path.join(W, ".fixtures-v2")
    if os.path.exists(marker):
        return
    srv, vid, site, ftproot = (os.path.join(W, d) for d in ("srv", "vid", "site", "ftproot"))
    log("fixtures: binary files")
    _rand(f"{srv}/f100.bin", 100_000_000)
    _rand(f"{srv}/f400.bin", 400_000_000)
    _rand(f"{srv}/big.zip", 20_000_000)
    _rand(f"{srv}/small.pdf", 300_000)
    _rand(f"{srv}/movie.mp4", 6_000_000)
    _rand(f"{srv}/song.mp3", 4_000_000)
    _rand(f"{srv}/setup.exe", 9_000_000)
    with open(f"{srv}/invoice.pdf", "wb") as f:                # a program disguised as a PDF
        f.write((b"MZ\x90\x00fake-exe-payload") * 2000)
    with open(f"{srv}/expired.zip", "wb") as f:                # login page saved as .zip
        f.write(b"<!DOCTYPE html><html><head><title>Login</title></head><body>Session expired"
                b"</body></html>")
    shutil.copy(f"{srv}/small.pdf", f"{srv}/report.pdf.exe")

    log("fixtures: videos and streams (ffmpeg)")
    os.makedirs(vid, exist_ok=True)
    m720, m360 = f"{vid}/movie720.mp4", f"{vid}/movie360.mp4"
    _ff(ff, "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=44100", "-t", "30", "-c:v", "libx264", "-preset",
        "ultrafast", "-g", "60", "-keyint_min", "60", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-b:a", "128k", "-shortest", m720)
    _ff(ff, "-i", m720, "-vf", "scale=640:360", "-c:v", "libx264", "-preset", "ultrafast",
        "-g", "60", "-c:a", "copy", m360)
    H = ["-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod"]
    for d in ("hls_plain", "hls_enc", "hls_fmp4", "hls_impl", "master", "dash"):
        os.makedirs(f"{vid}/{d}", exist_ok=True)
    _ff(ff, "-i", m720, "-c", "copy", *H, "-hls_segment_filename", f"{vid}/hls_plain/s%03d.ts",
        f"{vid}/hls_plain/index.m3u8")
    key = os.urandom(16)
    with open(f"{vid}/hls_enc/enc.key", "wb") as f:
        f.write(key)
    with open(f"{vid}/keyinfo.txt", "w") as f:
        f.write(f"enc.key\n{vid}/hls_enc/enc.key\n{os.urandom(16).hex()}\n".replace("\\", "/"))
    _ff(ff, "-i", m720, "-c", "copy", *H, "-hls_key_info_file", f"{vid}/keyinfo.txt",
        "-hls_segment_filename", f"{vid}/hls_enc/s%03d.ts", f"{vid}/hls_enc/index.m3u8")
    # implicit IV (= segment sequence number), encrypted by us per the HLS spec
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    shutil.copy(f"{vid}/hls_enc/enc.key", f"{vid}/hls_impl/enc.key")
    for i, seg in enumerate(sorted(glob.glob(f"{vid}/hls_plain/*.ts"))):
        pad = padding.PKCS7(128).padder()
        data = pad.update(open(seg, "rb").read()) + pad.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(i.to_bytes(16, "big"))).encryptor()
        with open(f"{vid}/hls_impl/{os.path.basename(seg)}", "wb") as f:
            f.write(enc.update(data) + enc.finalize())
    pl = open(f"{vid}/hls_plain/index.m3u8").read().replace(
        "#EXT-X-PLAYLIST-TYPE:VOD", '#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-KEY:METHOD=AES-128,'
                                    'URI="enc.key"')
    open(f"{vid}/hls_impl/index.m3u8", "w").write(pl)
    _ff(ff, "-i", m720, "-c", "copy", *H, "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename",
        f"{vid}/hls_fmp4/s%03d.m4s", f"{vid}/hls_fmp4/index.m3u8")
    _ff(ff, "-i", m720, "-an", "-c:v", "copy", *H, "-hls_segment_filename",
        f"{vid}/master/v720_%03d.ts", f"{vid}/master/v720.m3u8")
    _ff(ff, "-i", m360, "-an", "-c:v", "copy", *H, "-hls_segment_filename",
        f"{vid}/master/v360_%03d.ts", f"{vid}/master/v360.m3u8")
    _ff(ff, "-i", m720, "-vn", "-c:a", "copy", *H, "-hls_segment_filename",
        f"{vid}/master/a_%03d.ts", f"{vid}/master/audio.m3u8")
    open(f"{vid}/master/master.m3u8", "w").write(
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",'
        'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2",'
        'AUDIO="aud"\nv360.m3u8\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",'
        'AUDIO="aud"\nv720.m3u8\n')
    open(f"{vid}/drm.m3u8", "w").write(
        '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",'
        'KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:2.0,\nhls_plain/s000.ts\n'
        '#EXT-X-ENDLIST\n')
    _ff(ff, "-i", m720, "-map", "0:v", "-map", "0:a", "-c", "copy", "-f", "dash",
        "-seg_duration", "2", "-use_template", "1", "-use_timeline", "1",
        f"{vid}/dash/manifest.mpd")
    _ff(ff, "-i", m720, "-an", "-c", "copy", f"{vid}/v_only.mp4")
    _ff(ff, "-i", m720, "-vn", "-c", "copy", f"{vid}/a_only.m4a")
    shutil.copy(m720, f"{vid}/fresh_copy.mp4")
    open(f"{vid}/page.html", "w").write(
        '<html><head><title>Test Video Page</title></head><body><h1>Demo</h1>'
        '<video controls src="movie720.mp4"></video>'
        '<a href="docs/manual.pdf">Manual</a> <a href="files/a.zip">A</a> '
        '<a href="files/b.zip">B</a> <img src="img/pic1.png" alt="pic"> '
        '<a href="sub/page2.html">more</a> <a href="https://other.example/x.zip">ext</a>'
        '</body></html>')

    log("fixtures: grabber, checksums, archives")
    _rand(f"{vid}/docs/manual.pdf", 200_000)
    _rand(f"{vid}/files/a.zip", 300_000)
    _rand(f"{vid}/files/b.zip", 300_000)
    _rand(f"{vid}/img/pic1.png", 5_000)
    _rand(f"{vid}/sub/deep.mp3", 5_000)
    _rand(f"{vid}/sub/lvl3.iso", 1_000)

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()
    open(f"{vid}/files/SHA256SUMS", "w").write(
        f"{sha(f'{vid}/files/a.zip')}  a.zip\n{sha(f'{vid}/files/b.zip')}  b.zip\n")
    open(f"{vid}/docs/manual.pdf.sha256", "w").write(sha(f"{vid}/docs/manual.pdf") + "\n")
    open(f"{vid}/sub/page2.html", "w").write(
        '<html><body><a href="deep.mp3">song</a><a href="../files/a.zip">dup</a>'
        '<a href="page3.html">p3</a></body></html>')
    open(f"{vid}/sub/page3.html", "w").write('<html><body><a href="lvl3.iso">iso</a></body></html>')
    with zipfile.ZipFile(f"{vid}/good.zip", "w") as z:
        z.writestr("docs/readme.txt", "hello")
        z.writestr("data/a.bin", b"x" * 1000)
    with zipfile.ZipFile(f"{vid}/evil.zip", "w") as z:
        z.writestr("ok.txt", "fine")
        z.writestr("../../escape.txt", "pwned")
    sevenz = shutil.which("7z") or (r"C:\Program Files\7-Zip\7z.exe"
                                   if os.path.exists(r"C:\Program Files\7-Zip\7z.exe") else None)
    if sevenz:
        os.makedirs(f"{W}/tmp7z/inner", exist_ok=True)
        open(f"{W}/tmp7z/inner/hello.txt", "w").write("from 7z")
        subprocess.run([sevenz, "a", "-bd", f"{vid}/pack.7z", "inner"], cwd=f"{W}/tmp7z",
                       capture_output=True, check=True)
    shutil.copy(f"{srv}/f100.bin", f"{vid}/f100.bin")

    log("fixtures: e2e site + ftp")
    os.makedirs(site, exist_ok=True)
    shutil.copy(m720, f"{site}/movie720.mp4")
    shutil.copytree(f"{vid}/hls_plain", f"{site}/hls_plain", dirs_exist_ok=True)
    _rand(f"{site}/small.zip", 10_000)
    open(f"{site}/page.html", "w").write(
        '<!doctype html><html><head><title>E2E Test Page</title></head><body><h1>Downloads</h1>'
        '<a id="dyn" href="/download?id=1">Get the report</a> <a id="small" href="/small.zip">'
        'tiny</a> <a id="iso" href="/files/ubuntu.iso">iso</a> <a href="/files/part1.rar">p1</a>'
        ' <a href="/files/part2.rar">p2</a> <a href="/docs/guide.pdf">guide</a> '
        '<img src="/img/a.png" alt="pic"> <video id="v" width="640" height="360" autoplay muted '
        'playsinline src="/movie720.mp4"></video><script>fetch("/hls_plain/index.m3u8")</script>'
        '</body></html>')
    os.makedirs(ftproot, exist_ok=True)
    shutil.copy(f"{srv}/f100.bin", f"{ftproot}/f100.bin")
    shutil.copy(f"{srv}/big.zip", f"{ftproot}/big.zip")
    open(marker, "w").write("ok")
    log("fixtures: done")
