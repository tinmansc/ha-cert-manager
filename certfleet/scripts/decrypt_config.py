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

Safety notes (this is a recovery tool being run under pressure — it should
not be able to make a bad situation worse):
  - --out is refused if it resolves to the same file as either input, so a
    typo can't silently overwrite the live encrypted config.json or the key
    itself with a decrypted plaintext copy.
  - --out is refused if the target already exists, unless --force is given
    — no silent clobbering of an existing file.
  - The write is atomic (temp file + rename), so an interrupted write never
    leaves a half-written file at the target path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("Missing dependency. Run: pip install cryptography", file=sys.stderr)
    sys.exit(1)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key_file", help="Path to master.key")
    parser.add_argument("config_file", help="Path to config.json")
    parser.add_argument("--out", help="Write decrypted JSON here instead of stdout")
    parser.add_argument("--force", action="store_true",
                         help="Overwrite --out if it already exists")
    args = parser.parse_args()

    key_path = Path(args.key_file)
    config_path = Path(args.config_file)

    if not key_path.exists():
        print(f"Key file not found: {key_path}", file=sys.stderr)
        return 1
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    out_path = None
    if args.out:
        out_path = Path(args.out)
        # Refuse unconditionally — there is no legitimate reason to point
        # --out at either input file, and doing so by accident would
        # either destroy the key or silently downgrade the live encrypted
        # config to plaintext at the same path.
        for input_path, label in [(key_path, "the key file"), (config_path, "the config file")]:
            try:
                same = out_path.resolve() == input_path.resolve()
            except OSError:
                same = out_path.absolute() == input_path.absolute()
            if same:
                print(f"Refusing: --out must not be {label} itself ({input_path})", file=sys.stderr)
                return 1
        if out_path.exists() and not args.force:
            print(f"Refusing: {out_path} already exists. Pass --force to overwrite it.", file=sys.stderr)
            return 1

    raw = config_path.read_bytes()

    if raw[:1] == b"{":
        # Pre-1.0.10 plaintext config — nothing to decrypt.
        print("Note: this file is already plaintext (pre-1.0.10 format), no key needed.", file=sys.stderr)
        plaintext = raw
    else:
        # Strip whitespace/newlines — a very easy way to end up with a
        # "wrong key" error is a text editor appending a trailing newline
        # when the key was manually saved to a file.
        key = key_path.read_bytes().strip()
        try:
            fernet = Fernet(key)
        except Exception as exc:
            print(f"Key file does not contain a valid Fernet key: {exc}", file=sys.stderr)
            return 1
        try:
            plaintext = fernet.decrypt(raw)
        except InvalidToken:
            print(
                "Decryption failed — this key does not match this config.json.\n"
                "If you have a config.json.bak alongside it, try that file instead: "
                "it's the previous save, and may match a different key if a rotation "
                "didn't fully complete.",
                file=sys.stderr,
            )
            return 1

    try:
        # Re-serialize with indentation for readability, same as the app's
        # own writes. This also validates the decrypted bytes are actually
        # well-formed JSON before we write anything out.
        pretty = json.dumps(json.loads(plaintext), indent=2)
    except json.JSONDecodeError as exc:
        print(f"Decrypted successfully but the result isn't valid JSON: {exc}", file=sys.stderr)
        return 1

    if out_path:
        _atomic_write_text(out_path, pretty)
        print(f"Wrote decrypted config to {out_path}", file=sys.stderr)
    else:
        print(pretty)

    return 0


if __name__ == "__main__":
    sys.exit(main())
