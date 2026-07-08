"""Encrypted storage for the device configuration file.

The whole config.json payload is encrypted as a single Fernet token —
not field-by-field — so a new sensitive field added later (e.g. we
almost shipped field-level encryption and forgot to cover `username`)
is protected by construction instead of relying on someone remembering
to flag it as sensitive.

Key material lives in its own file (master.key) next to config.json,
both under /config/ha_cert_manager/. That directory is what Home
Assistant backs up and restores as a unit. The key is deliberately NOT
derived from SUPERVISOR_TOKEN: that token is reissued by the Supervisor
per add-on install/start and is not guaranteed to match after a backup
restore onto a fresh Home Assistant instance. A key that travels in the
same backup archive as the data it protects is the only kind that
reliably survives disaster recovery.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

from cryptography.fernet import Fernet, InvalidToken

CONFIG_DIR = Path(os.environ.get("OPTIONS_FILE", "/config/ha_cert_manager/config.json")).parent
KEY_FILE = CONFIG_DIR / "master.key"
CONFIG_FILE = CONFIG_DIR / "config.json"       # stores a Fernet token, not raw JSON, once encrypted
CONFIG_BACKUP = CONFIG_DIR / "config.json.bak"  # one-deep backup, refreshed on every save

LogFn = Optional[Callable[[str, str], None]]  # (level, message) — matches devices/base.Logger


class DecryptionError(Exception):
    """Raised when config.json can't be decrypted with the key we have."""


def _read_bytes_verified(path: Path, attempts: int = 3) -> bytes:
    """Read a small file into two separate buffers and require they match.

    Defends against a single corrupted read (torn read, flaky storage,
    a bit-flip in the read buffer) turning into a silently-wrong key or
    ciphertext. A transient glitch should clear on retry; a read that
    stays inconsistent across several attempts means the underlying
    storage itself is unreliable, which is worth surfacing rather than
    quietly acting on possibly-corrupt bytes.
    """
    for _ in range(attempts):
        a = path.read_bytes()
        b = path.read_bytes()
        if a == b:
            return a
    raise IOError(f"Inconsistent reads from {path} after {attempts} attempts — possible storage or memory fault")


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` without ever leaving a half-written file on disk.

    Writes to a sibling temp file, flushes and fsyncs it, then renames it
    over the target in a single filesystem operation (atomic on both
    POSIX and Windows). A crash or power loss mid-write leaves either the
    old file or the new one — never a truncated hybrid of both. This is
    the real defense against write corruption; re-reading a file you just
    wrote does not catch a torn write, only atomic replace does.
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def generate_key() -> bytes:
    return Fernet.generate_key()


def ensure_key() -> bytes:
    """Return the master key, generating one on first run."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        key = generate_key()
        _atomic_write(KEY_FILE, key)
        return key
    return _read_bytes_verified(KEY_FILE)


def encrypt_config(data: bytes, key: Optional[bytes] = None) -> bytes:
    return Fernet(key or ensure_key()).encrypt(data)


def decrypt_config(token: bytes, key: Optional[bytes] = None, logger: LogFn = None) -> bytes:
    """Decrypt a config token.

    If no explicit key is given, we use the currently active key but —
    on failure — re-read the key fresh from disk and retry once before
    giving up. This guards against a stale or bit-flipped in-memory copy
    of the key (plausible on SBC hardware like a Pi 3 running hot with no
    ECC RAM) being mistaken for genuine data corruption. If an explicit
    key IS given (e.g. testing a manually pasted candidate key), no
    fallback makes sense — the caller already knows exactly what key
    they meant to try.
    """
    active_key = key if key is not None else ensure_key()
    try:
        return Fernet(active_key).decrypt(token)
    except InvalidToken:
        if key is not None:
            raise DecryptionError("Decryption failed with the provided key")
        fresh_key = _read_bytes_verified(KEY_FILE)
        try:
            result = Fernet(fresh_key).decrypt(token)
            if logger:
                logger("warning", "Key decryption failed on first attempt, recovered after "
                                   "re-reading the key from disk — possible transient memory error")
            return result
        except InvalidToken as exc:
            raise DecryptionError(
                "Decryption failed even after re-reading the key from disk — "
                "the key or the config file is genuinely corrupted"
            ) from exc


def load_config(logger: LogFn = None) -> dict:
    """Load and decrypt the config file. Returns {} if none exists yet.

    Transparently migrates a pre-encryption plaintext config.json (from
    versions before whole-file encryption shipped) into the new encrypted
    format on first read after upgrade — existing installs keep working
    without any manual step.
    """
    if not CONFIG_FILE.exists():
        return {}
    raw = CONFIG_FILE.read_bytes()
    # Plaintext configs are JSON objects starting with '{'. Fernet tokens are
    # urlsafe-base64 and can never start with that byte — cheap, reliable test.
    if raw[:1] == b"{":
        data = json.loads(raw)
        if logger:
            logger("info", "Migrating plaintext config.json to encrypted storage")
        save_config(data, logger=logger)
        return data
    plaintext = decrypt_config(raw, logger=logger)
    return json.loads(plaintext)


def save_config(data: dict, logger: LogFn = None) -> None:
    """Encrypt and atomically write the config file, keeping a one-deep backup."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = encrypt_config(json.dumps(data, indent=2).encode())
    if CONFIG_FILE.exists():
        _atomic_write(CONFIG_BACKUP, CONFIG_FILE.read_bytes())
    _atomic_write(CONFIG_FILE, token)


def rotate_key(logger: LogFn = None) -> None:
    """Generate a brand-new random key and re-encrypt the existing config under it.

    Safe — no data is lost, and there is nothing for a human to type or
    paste, so there's no typo risk to guard against here. The new
    ciphertext is written to disk before the new key file replaces the
    old one, so a crash mid-rotation still leaves a consistent
    (old-key, old-ciphertext) or (new-key, new-ciphertext) pair, never a
    mismatched combination.
    """
    data = load_config(logger=logger)
    new_key = generate_key()
    token = Fernet(new_key).encrypt(json.dumps(data, indent=2).encode())
    if CONFIG_FILE.exists():
        _atomic_write(CONFIG_BACKUP, CONFIG_FILE.read_bytes())
    _atomic_write(CONFIG_FILE, token)
    _atomic_write(KEY_FILE, new_key)
    if logger:
        logger("info", "Encryption key rotated — all stored credentials re-encrypted")


def set_key(new_key_str: str, force: bool = False, logger: LogFn = None) -> dict:
    """Adopt a manually-provided key — covers both "restore a backed-up key"
    and "reset after losing the key", unified into one flow:

      1. Try to decrypt the *current* config.json with the pasted key.
      2. If it works, this was a legitimate restore — adopt the key, keep
         all existing devices, nothing is lost.
      3. If it doesn't work, refuse unless `force=True`. Forcing adopts
         the key anyway and resets config to empty — this is the
         destructive path the UI must gate behind a typed "NO RECOVERY"
         confirmation, since it permanently discards every stored
         credential.

    Returns the resulting config dict (recovered devices, or {} if reset).
    """
    try:
        new_key = new_key_str.encode()
        Fernet(new_key)  # raises if this isn't a valid 32-byte urlsafe-base64 Fernet key
    except Exception as exc:
        raise ValueError("Not a valid encryption key") from exc

    recovered: dict = {}
    if CONFIG_FILE.exists():
        raw = CONFIG_FILE.read_bytes()
        try:
            recovered = json.loads(decrypt_config(raw, key=new_key))
            if logger:
                logger("info", "Encryption key restored — existing config decrypted successfully")
        except DecryptionError:
            if not force:
                raise
            if logger:
                logger("warning", "Encryption key replaced WITHOUT recovering existing config — "
                                   "all previously stored credentials are now unreadable, config reset to empty")

    _atomic_write(KEY_FILE, new_key)
    save_config(recovered, logger=logger)
    return recovered
