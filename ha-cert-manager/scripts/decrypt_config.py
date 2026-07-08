#!/usr/bin/env python3
"""Standalone disaster-recovery tool — decrypt config.json without the app running.

Config.json is encrypted with Fernet (from Python's `cryptography` package):
AES-128-CBC + HMAC-SHA256 under a documented token format. This script does
exactly what the app itself does to read it, with no dependency on any other
file in this repo, so it still works if the add-on itself is broken, won't
start, or you've copied just these two files off the device entirely.

Usage:
    pip install cryptography   # if not already available
    python3 decrypt_config.py master.key config.json
    python3 decrypt_config.py master.key config.json --out recovered.json

If config.json is still in the old pre-1.0.10 plaintext format (starts with
a literal '{'), it's printed/copied as-is — no key needed for that case.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("Missing dependency. Run: pip install cryptography", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key_file", help="Path to master.key")
    parser.add_argument("config_file", help="Path to config.json")
    parser.add_argument("--out", help="Write decrypted JSON here instead of stdout")
    args = parser.parse_args()

    key_path = Path(args.key_file)
    config_path = Path(args.config_file)

    if not key_path.exists():
        print(f"Key file not found: {key_path}", file=sys.stderr)
        return 1
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    raw = config_path.read_bytes()

    if raw[:1] == b"{":
        # Pre-1.0.10 plaintext config — nothing to decrypt.
        print("Note: this file is already plaintext (pre-1.0.10 format), no key needed.", file=sys.stderr)
        plaintext = raw
    else:
        key = key_path.read_bytes()
        try:
            plaintext = Fernet(key).decrypt(raw)
        except InvalidToken:
            print(
                "Decryption failed — this key does not match this config.json.\n"
                "If you have a config.json.bak alongside it, try that file instead: "
                "it's the previous save, and may match a different key if a rotation "
                "didn't fully complete.",
                file=sys.stderr,
            )
            return 1

    # Re-serialize with indentation for readability, same as the app's own writes.
    pretty = json.dumps(json.loads(plaintext), indent=2)

    if args.out:
        Path(args.out).write_text(pretty)
        print(f"Wrote decrypted config to {args.out}", file=sys.stderr)
    else:
        print(pretty)

    return 0


if __name__ == "__main__":
    sys.exit(main())
