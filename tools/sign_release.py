"""Sign PyDM releases.

  python tools/sign_release.py keygen                      # once: creates release_key.pem
  python tools/sign_release.py sign PyDM-Setup.exe 3.1 https://…/PyDM-Setup.exe "notes"
      → writes manifest.json and manifest.json.sig; upload both next to each other.

Keep release_key.pem secret and offline. Paste the printed public key into pydm/release.py.
"""
import base64
import hashlib
import json
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def keygen(path="release_key.pem"):
    key = Ed25519PrivateKey.generate()
    with open(path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    print("Private key saved to", path, "(keep it secret)")
    print("PUBLIC_KEY =", repr(base64.b64encode(pub).decode()))


def sign(installer, version, url, notes="", key_path="release_key.pem", out="manifest.json"):
    h = hashlib.sha256()
    with open(installer, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    manifest = json.dumps({"version": version, "url": url, "sha256": h.hexdigest(),
                           "notes": notes}, indent=1).encode()
    with open(key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), None)
    with open(out, "wb") as f:
        f.write(manifest)
    with open(out + ".sig", "w") as f:
        f.write(base64.b64encode(key.sign(manifest)).decode())
    print("Wrote", out, "and", out + ".sig")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "keygen":
        keygen(*sys.argv[2:3])
    elif len(sys.argv) >= 5 and sys.argv[1] == "sign":
        sign(*sys.argv[2:6])
    else:
        print(__doc__)
