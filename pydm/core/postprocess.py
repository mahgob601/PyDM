"""After-download actions: safe archive extraction and a user-defined command."""
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

from . import security
from .util import is_inside

MAX_TOTAL = 64 * 1024 ** 3        # refuse archives that expand beyond 64 GB
MAX_FILES = 200_000
BOMB_RATIO = 250                  # a 1 MB entry claiming > 250 MB is treated as a zip bomb
ARCHIVE_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar", ".zip",
                ".7z", ".rar")


class ExtractError(Exception):
    pass


def archive_base(name):
    low = name.lower()
    for ext in ARCHIVE_EXTS:
        if low.endswith(ext):
            return name[:-len(ext)], ext
    return None, None


def is_archive(name):
    return archive_base(name)[0] is not None


def _unique_dir(path):
    cand, i = path, 1
    while os.path.exists(cand):
        cand = f"{path} ({i})"
        i += 1
    return cand


def _find_7z():
    for name in ("7z", "7za", "7zz"):
        p = shutil.which(name)
        if p:
            return p
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            p = os.path.join(base, "7-Zip", "7z.exe")
            if os.path.isfile(p):
                return p
    return None


def _extract_zip(archive, dest):
    total = files = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ":" in name.split("/")[0] or ".." in name.split("/"):
                raise ExtractError(f"Unsafe path in archive: {info.filename!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                continue                                  # never create symlinks
            files += 1
            total += info.file_size
            if files > MAX_FILES or total > MAX_TOTAL:
                raise ExtractError("Archive expands to an unsafe size (possible zip bomb)")
            if info.file_size > 100 * 1024 ** 2 and info.compress_size and \
                    info.file_size / info.compress_size > BOMB_RATIO:
                raise ExtractError("Archive compression ratio is suspicious (possible zip bomb)")
            target = os.path.join(dest, *[p for p in name.split("/") if p])
            if not is_inside(target, dest):
                raise ExtractError(f"Unsafe path in archive: {info.filename!r}")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            written = 0
            with zf.open(info) as src, open(target, "wb") as dst:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    written += len(block)
                    if written > info.file_size + 1024:   # header lied about the size
                        raise ExtractError("Archive entry is larger than declared (zip bomb)")
                    dst.write(block)
    return files


def _extract_tar(archive, dest):
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        if len(members) > MAX_FILES or sum(m.size for m in members) > MAX_TOTAL:
            raise ExtractError("Archive expands to an unsafe size")
        safe = []
        for m in members:
            if m.issym() or m.islnk() or m.isdev():
                continue
            if not is_inside(os.path.join(dest, m.name), dest):
                raise ExtractError(f"Unsafe path in archive: {m.name!r}")
            safe.append(m)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=safe, filter="data")
        else:
            tf.extractall(dest, members=safe)  # noqa: S202 (members validated above)
    return len(safe)


def _extract_7z(archive, dest):
    exe = _find_7z()
    if not exe:
        raise ExtractError("Install 7-Zip to extract .7z and .rar files")
    r = subprocess.run([exe, "x", "-y", "-bd", f"-o{dest}", archive], capture_output=True,
                       timeout=3600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode not in (0, 1):
        raise ExtractError(f"7-Zip failed (code {r.returncode})")
    count = 0
    for root, dirs, fs in os.walk(dest):
        for f in fs + dirs:
            p = os.path.join(root, f)
            if os.path.islink(p) or not is_inside(p, dest):
                os.remove(p) if os.path.islink(p) else None
                continue
            count += 1
    return count


def extract(archive, motw_url=None, referrer=None):
    """Extract next to the archive into a new folder. Returns (folder, file_count)."""
    base, ext = archive_base(os.path.basename(archive))
    if not base:
        raise ExtractError("Not a supported archive")
    dest = _unique_dir(os.path.join(os.path.dirname(archive), base))
    os.makedirs(dest)
    try:
        if ext == ".zip":
            count = _extract_zip(archive, dest)
        elif ext in (".7z", ".rar"):
            count = _extract_7z(archive, dest)
        else:
            count = _extract_tar(archive, dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if motw_url and sys.platform.startswith("win"):
        n = 0
        for root, _dirs, fs in os.walk(dest):
            for f in fs:
                security.write_mark_of_the_web(os.path.join(root, f), motw_url, referrer)
                n += 1
                if n > 5000:
                    break
    return dest, count


def run_command(template, path, url):
    """Run the user's post-download command. Placeholders are substituted per argument, never
    through a shell, so file names and URLs can't inject extra commands."""
    args = shlex.split(template, posix=not sys.platform.startswith("win"))
    if not args:
        return None
    values = {"{path}": path, "{folder}": os.path.dirname(path),
              "{name}": os.path.basename(path), "{url}": url}
    out = []
    for a in args:
        a = a.strip('"') if sys.platform.startswith("win") else a
        for k, v in values.items():
            a = a.replace(k, v)
        out.append(a)
    return subprocess.Popen(out, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
