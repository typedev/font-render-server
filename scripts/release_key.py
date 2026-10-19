"""Release signing key: generate a key pair, sign and verify SHA256SUMS.

Maintainer tool, not part of the server package (see docs/releasing.md):

    uv run --with cryptography scripts/release_key.py generate [--out FILE]
    uv run --with cryptography scripts/release_key.py public  [--key FILE]
    uv run --with cryptography scripts/release_key.py sign    SHA256SUMS [--key FILE]
    uv run --with cryptography scripts/release_key.py verify  SHA256SUMS [SIG] [--public B64]

The private key is read from --key FILE or the RELEASE_SIGNING_KEY environment variable
(base64 of the 32-byte Ed25519 seed). The signature file (SHA256SUMS.sig) is JSON:
{"key_id": "<sha256(public key)[:16]>", "signature": "<base64>"}.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# Repository root: scripts/release_key.py -> parents[1]
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEYS_FILE = ROOT / "src" / "font_render" / "release_key.py"


def key_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _public_raw(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _load_private(path: str | None) -> Ed25519PrivateKey:
    if path:
        raw = Path(path).read_text(encoding="ascii").strip()
    else:
        raw = os.environ.get("RELEASE_SIGNING_KEY", "").strip()
        if not raw:
            sys.exit("no key: pass --key FILE or set RELEASE_SIGNING_KEY")
    seed = base64.b64decode(raw, validate=True)
    if len(seed) != 32:
        sys.exit("key must be base64 of 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def _trusted_keys() -> dict[str, str]:
    """key_id -> base64 public key, from src/font_render/release_key.py (if it exists)."""
    if not PUBLIC_KEYS_FILE.is_file():
        return {}
    ns: dict = {}
    exec(PUBLIC_KEYS_FILE.read_text(encoding="utf-8"), ns)
    return {key_id(base64.b64decode(k)): k for k in ns.get("TRUSTED_KEYS", [])}


def cmd_generate(args):
    out = Path(args.out)
    if out.exists():
        sys.exit(f"{out} exists, refusing to overwrite")
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(base64.b64encode(seed).decode() + "\n")
    public_raw = _public_raw(private)
    print(f"private key written to {out} (keep it secret, see docs/releasing.md)")
    print(f"public key: {base64.b64encode(public_raw).decode()}")
    print(f"key id:     {key_id(public_raw)}")


def cmd_public(args):
    public_raw = _public_raw(_load_private(args.key))
    print(f"public key: {base64.b64encode(public_raw).decode()}")
    print(f"key id:     {key_id(public_raw)}")


def cmd_sign(args):
    private = _load_private(args.key)
    data = Path(args.file).read_bytes()
    sig = {"key_id": key_id(_public_raw(private)),
           "signature": base64.b64encode(private.sign(data)).decode()}
    out = Path(args.file + ".sig")
    out.write_text(json.dumps(sig) + "\n", encoding="ascii")
    print(f"signed {args.file} with key {sig['key_id']} -> {out}")


def cmd_verify(args):
    sig = json.loads(Path(args.sig or args.file + ".sig").read_text(encoding="ascii"))
    if args.public:
        keys = {key_id(base64.b64decode(args.public)): args.public}
    else:
        keys = _trusted_keys()
    public = keys.get(sig.get("key_id", ""))
    if public is None:
        sys.exit(f"FAIL: key {sig.get('key_id')} is not trusted")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public)).verify(
            base64.b64decode(sig["signature"]), Path(args.file).read_bytes())
    except InvalidSignature:
        sys.exit("FAIL: bad signature")
    print(f"OK: {args.file} signed by key {sig['key_id']}")


def main():
    parser = argparse.ArgumentParser(description="Release signing key (Ed25519)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("generate", help="create a new key pair")
    p.add_argument("--out", default="release-signing-key.txt")
    p.set_defaults(func=cmd_generate)
    p = sub.add_parser("public", help="print the public key of a private key")
    p.add_argument("--key")
    p.set_defaults(func=cmd_public)
    p = sub.add_parser("sign", help="write FILE.sig")
    p.add_argument("file")
    p.add_argument("--key")
    p.set_defaults(func=cmd_sign)
    p = sub.add_parser("verify", help="check FILE against FILE.sig")
    p.add_argument("file")
    p.add_argument("sig", nargs="?")
    p.add_argument("--public", help="base64 public key (default: TRUSTED_KEYS in the repo)")
    p.set_defaults(func=cmd_verify)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
