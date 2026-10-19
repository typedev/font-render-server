"""Public keys that sign releases (SHA256SUMS.sig); see docs/releasing.md.

Updates are installed only if their SHA256SUMS is signed by one of these keys. Rotation
adds the new key next to the old one first, so installed servers learn it from a release
signed with the old key.
"""

# base64 of 32-byte Ed25519 public keys; comment: key id (scripts/release_key.py), created
TRUSTED_KEYS: list[str] = [
    "pZ324jLYI6ukn2E+oejxs6kudovbkuJmhahcpNAF9Ck=",  # 4fdea217d5c9d919, created 2026-10-18
]
