"""Release configuration for official builds.

UPDATE_URL  : signed manifest of the newest GitHub release (latest.json.sig sits next to it).
              GitHub's /releases/latest/download/ always points at the newest release.
PUBLIC_KEY  : Ed25519 public key. The matching private key signs every release (GitHub secret
              RELEASE_SIGNING_KEY); a manifest it didn't sign is refused.
"""
UPDATE_URL = "https://github.com/mahgob601/PyDM/releases/latest/download/latest.json"
PUBLIC_KEY = "4U69klyZF8bQvEiP0iDAAbyzQWCUFyRA15xwgWzq7kQ="
