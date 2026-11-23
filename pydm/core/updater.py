"""Signed auto-updates.

Release flow (see tools/sign_release.py):
  1. build the installer, upload it (e.g. GitHub Releases)
  2. write manifest.json {"version", "url", "sha256", "notes"} and sign it with your Ed25519
     private key → manifest.json.sig
  3. upload both; set UPDATE_URL and PUBLIC_KEY in pydm/release.py (or Settings)

PyDM only offers an update when the manifest signature verifies against the public key built
into the app, the version is newer, and the URLs are HTTPS. The installer is then downloaded
by the normal engine with the manifest's SHA-256 enforced — any mismatch is flagged as a
threat and never offered for install.
"""
import base64
import json
import re
import urllib.parse
import urllib.request

from .util import VERSION


class UpdateError(Exception):
    pass


def parse_version(v):
    parts = [int(x) for x in re.findall(r"\d+", str(v))[:4]]
    while len(parts) > 1 and parts[-1] == 0:      # "4.1" == "4.1.0": never an update loop
        parts.pop()
    return tuple(parts) or (0,)


def verify_signature(public_key_b64, payload, signature_b64):
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as e:
        raise UpdateError("Install the 'cryptography' package to enable secure updates") from e
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64.strip()), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def _get(url, opener, limit=256 * 1024):
    if urllib.parse.urlsplit(url).scheme != "https" and not url.startswith("http://127.0.0.1"):
        raise UpdateError("Update URLs must use HTTPS")
    req = urllib.request.Request(url, headers={"User-Agent": f"PyDM/{VERSION}"})
    with opener.open(req, timeout=20) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("Update manifest is too large")
    return data


def launch_installer(path, wait_pid=None):
    """Run a verified PyDM installer as an in-place upgrade once this process has exited.

    The caller quits right after this returns. A hidden PowerShell waits for our PID to be gone
    (a silent install refuses to run while PyDM holds its mutex), then runs setup /SILENT; the
    installer starts PyDM again when it's done."""
    import os
    import subprocess
    import sys
    if not sys.platform.startswith("win"):
        raise UpdateError("Automatic install is only available on Windows")
    if not os.path.isfile(path):
        raise UpdateError("Installer not found")
    pid = int(wait_pid or os.getpid())
    q = path.replace("'", "''")
    script = (f"Wait-Process -Id {pid} -Timeout 120 -ErrorAction SilentlyContinue; "
              f"Start-Process -FilePath '{q}' -ArgumentList "
              "'/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS'")
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
             getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                      "Bypass", "-WindowStyle", "Hidden", "-Command", script],
                     creationflags=flags, close_fds=True)


def check(update_url, public_key, opener=None, current=VERSION):
    """Returns None (up to date) or the verified manifest dict."""
    if not update_url or not public_key:
        raise UpdateError("Updates aren't configured for this build")
    opener = opener or urllib.request.build_opener()
    payload = _get(update_url, opener)
    sig = _get(update_url + ".sig", opener, 4096).decode("ascii", "replace")
    if not verify_signature(public_key, payload, sig):
        raise UpdateError("The update manifest's signature is invalid — update refused")
    try:
        manifest = json.loads(payload)
    except ValueError as e:
        raise UpdateError("Update manifest is not valid JSON") from e
    for k in ("version", "url", "sha256"):
        if k not in manifest:
            raise UpdateError(f"Update manifest is missing '{k}'")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["sha256"]).lower()):
        raise UpdateError("Update manifest has an invalid SHA-256")
    if urllib.parse.urlsplit(manifest["url"]).scheme != "https" and \
            not manifest["url"].startswith("http://127.0.0.1"):
        raise UpdateError("Update download must use HTTPS")
    if parse_version(manifest["version"]) <= parse_version(current):
        return None
    return manifest
