"""Encrypted storage for the device configuration file.

The whole config.json payload is encrypted as a single Fernet token —
not field-by-field — so a new sensitive field added later (e.g. we
almost shipped field-level encryption and forgot to cover `username`)
is protected by construction instead of relying on someone remembering
to flag it as sensitive.

Key material lives in its own file (master.key) next to config.json,
both under /config/certfleet/. That directory is what Home
Assistant backs up and restores as a unit. The key is deliberately NOT
derived from SUPERVISOR_TOKEN: that token is reissued by the Supervisor
per add-on install/start and is not guaranteed to match after a backup
restore onto a fresh Home Assistant instance. A key that travels in the
same backup archive as the data it protects is the only kind that
reliably survives disaster recovery.

This module is also the last line of defense on hardware that's
genuinely more failure-prone than a proper server — Raspberry Pi SD
cards corrupt, go read-only, or die outright far more often than a
RAID array with scheduled backups does. Every write here assumes that
can happen mid-operation, and tries to leave things recoverable rather
than silently wrong.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

from cryptography.fernet import Fernet, InvalidToken

CONFIG_DIR = Path(os.environ.get("OPTIONS_FILE", "/config/certfleet/config.json")).parent
KEY_FILE = CONFIG_DIR / "master.key"
CONFIG_FILE = CONFIG_DIR / "config.json"       # stores a Fernet token, not raw JSON, once encrypted
CONFIG_BACKUP = CONFIG_DIR / "config.json.bak"  # one-deep backup, refreshed on every save

# Pre-rename add-on slug — this app was called "HA Cert Manager" (slug
# ha_cert_manager) before being renamed to CertFleet. Kept only so
# migrate_from_old_slug() can find and copy forward an existing install;
# nothing else should ever reference this path.
_OLD_SLUG_CONFIG_DIR = Path("/config/ha_cert_manager")

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


def _check_writable() -> None:
    """Verify the config directory is actually writable before mutating
    anything in it.

    This is the fix for "what if we can read the key but not write it":
    a read-only SD card (a common Pi failure mode, not a hypothetical
    one) would otherwise let a rotation succeed partway — e.g. write the
    new encrypted config.json but then fail to write the new master.key
    — leaving the two files permanently out of sync with each other.
    Checking writability first means a rotation either fully succeeds or
    touches nothing at all.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    probe = CONFIG_DIR / ".write_test.tmp"
    try:
        probe.write_bytes(b"probe")
        probe.unlink()
    except OSError as exc:
        raise OSError(f"{CONFIG_DIR} is not writable: {exc}") from exc


def migrate_from_old_slug(logger: LogFn = None) -> None:
    """Copy master.key/config.json/config.json.bak forward from the old
    ha_cert_manager add-on directory, if this (certfleet) directory
    doesn't have its own data yet.

    Home Assistant's Supervisor treats a changed add-on slug as installing
    a brand-new add-on, not renaming the existing one — without this, an
    instance with real devices already configured would boot up under the
    new slug looking completely empty, with the real data orphaned at the
    old path. This only ever copies forward and never touches or deletes
    the old directory, so the original files are never at risk even if
    something goes wrong partway through.
    """
    if KEY_FILE.exists() or CONFIG_FILE.exists():
        return  # this install already has its own data — nothing to migrate
    if not _OLD_SLUG_CONFIG_DIR.exists():
        return  # nothing to migrate from (fresh install, or already migrated)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("master.key", "config.json", "config.json.bak"):
        src = _OLD_SLUG_CONFIG_DIR / name
        if src.exists():
            _atomic_write(CONFIG_DIR / name, src.read_bytes())
            copied.append(name)

    if copied and logger:
        logger("info", f"Migrated {', '.join(copied)} from the old ha_cert_manager "
                        f"add-on directory after the app was renamed to CertFleet — "
                        f"existing devices and credentials should be intact")


def generate_key() -> bytes:
    return Fernet.generate_key()


def _read_key_file() -> bytes:
    """Read master.key with the dual-buffer integrity check, then strip.

    Stripped in case master.key was ever hand-edited (SSH + a text editor
    is a documented recovery path — see DOCS.md) and picked up a trailing
    newline or space. The integrity check still runs on the raw bytes as
    read, so this doesn't mask genuine corruption — it's a normalization
    step, not a substitute for it. Every place that reads KEY_FILE for use
    as a Fernet key goes through this single function.
    """
    return _read_bytes_verified(KEY_FILE).strip()


def ensure_key(logger: LogFn = None) -> bytes:
    """Return the master key, generating one on first run.

    If an ENCRYPTED config.json already exists at this point, generating a
    new key silently would leave that file permanently unreadable — it was
    encrypted under whatever key used to be here, which is now gone. That's
    still the only thing we CAN do (there's no key to recover), but it
    should never happen quietly: log it loudly so "why are my devices gone"
    has an answer in the event log instead of a mystery.

    A config.json that's still plaintext (starts with '{') is NOT that
    situation — it's the normal legacy-migration path (load_config() is
    about to encrypt it for the first time via save_config()) and doesn't
    mean anything was lost. Checking the byte content, not just existence,
    avoids alarming users with a false "unrecoverable data loss" error on
    every legacy plaintext config's first-ever encryption.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        is_genuinely_encrypted = CONFIG_FILE.exists() and CONFIG_FILE.read_bytes()[:1] != b"{"
        if is_genuinely_encrypted and logger:
            logger("error", "Generating a new encryption key, but an existing config.json was found — "
                             "it was encrypted under a different key that is now missing and cannot be "
                             "recovered. If you have a backup of the old master.key, restore it via "
                             "Settings -> Encryption Key before making any further changes.")
        key = generate_key()
        _atomic_write(KEY_FILE, key)
        return key
    return _read_key_file()


def encrypt_config(data: bytes, key: Optional[bytes] = None, logger: LogFn = None) -> bytes:
    return Fernet(key or ensure_key(logger=logger)).encrypt(data)


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
    active_key = key if key is not None else ensure_key(logger=logger)
    try:
        return Fernet(active_key).decrypt(token)
    except InvalidToken:
        if key is not None:
            raise DecryptionError("Decryption failed with the provided key")
        fresh_key = _read_key_file()
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


def _parse_or_decrypt(raw: bytes, logger: LogFn = None) -> dict:
    """Shared plaintext-or-encrypted parsing used for both config.json and
    config.json.bak, so the backup gets exactly the same handling as the
    live file rather than a second, slightly-different code path."""
    if raw[:1] == b"{":
        return json.loads(raw)
    return json.loads(decrypt_config(raw, logger=logger))


def load_config(logger: LogFn = None) -> dict:
    """Load and decrypt the config file. Returns {} if none exists yet.

    Transparently migrates a pre-encryption plaintext config.json (from
    versions before whole-file encryption shipped) into the new encrypted
    format on first read after upgrade — existing installs keep working
    without any manual step.

    Self-heals from config.json.bak in two situations, both logged loudly
    rather than silently:
      - config.json is missing entirely, but a backup exists (accidental
        deletion, a corrupted write that never got finished).
      - config.json exists but won't decrypt with the current key, while
        the backup does — this is the signature of an interrupted key
        rotation (new ciphertext written, new key write failed partway).
    """
    if not CONFIG_FILE.exists():
        if CONFIG_BACKUP.exists():
            if logger:
                logger("warning", "config.json is missing but config.json.bak exists — "
                                   "attempting to recover from the backup")
            try:
                data = _parse_or_decrypt(CONFIG_BACKUP.read_bytes(), logger)
            except DecryptionError as exc:
                raise DecryptionError(
                    "config.json is missing and config.json.bak could not be decrypted either "
                    "— no automatic recovery is possible"
                ) from exc
            save_config(data, logger=logger)
            if logger:
                logger("info", "Recovered config.json from config.json.bak")
            return data
        return {}

    raw = CONFIG_FILE.read_bytes()
    if raw[:1] == b"{":
        data = json.loads(raw)
        if logger:
            logger("info", "Migrating plaintext config.json to encrypted storage")
        save_config(data, logger=logger)
        return data

    try:
        return json.loads(decrypt_config(raw, logger=logger))
    except DecryptionError:
        if CONFIG_BACKUP.exists():
            try:
                backup_raw = CONFIG_BACKUP.read_bytes()
                if backup_raw[:1] != b"{":
                    data = json.loads(decrypt_config(backup_raw, logger=logger))
                    if logger:
                        logger("warning", "config.json did not match the current key, but "
                                           "config.json.bak did — likely an interrupted key "
                                           "rotation. Restoring from the backup automatically.")
                    save_config(data, logger=logger)
                    return data
            except DecryptionError:
                pass
        raise


def save_config(data: dict, logger: LogFn = None) -> None:
    """Encrypt and atomically write the config file, keeping a one-deep backup."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = encrypt_config(json.dumps(data, indent=2).encode(), logger=logger)
    if CONFIG_FILE.exists():
        _atomic_write(CONFIG_BACKUP, CONFIG_FILE.read_bytes())
    _atomic_write(CONFIG_FILE, token)


def rotate_key(logger: LogFn = None) -> None:
    """Generate a brand-new random key and re-encrypt the existing config under it.

    No data is lost as long as the writes succeed — and now we check
    that up front (_check_writable) and prove it after the fact (the
    verify step below) instead of just hoping. There is nothing for a
    human to type or paste here, so there's no typo risk to guard
    against, unlike Restore/Set Key.
    """
    _check_writable()
    data = load_config(logger=logger)
    new_key = generate_key()
    token = Fernet(new_key).encrypt(json.dumps(data, indent=2).encode())
    if CONFIG_FILE.exists():
        _atomic_write(CONFIG_BACKUP, CONFIG_FILE.read_bytes())
    _atomic_write(CONFIG_FILE, token)
    _atomic_write(KEY_FILE, new_key)

    # Self-verify: prove the round trip actually works before reporting
    # success, rather than assuming both writes landed correctly. This is
    # an internal write-consistency check, not "wrong key" — a plain
    # RuntimeError so callers don't mistake it for the retry-with-force
    # case that DecryptionError normally signals.
    try:
        verify_raw = CONFIG_FILE.read_bytes()
        verify_key = _read_key_file()
        Fernet(verify_key).decrypt(verify_raw)
    except InvalidToken as exc:
        raise RuntimeError(
            "Key rotation wrote new files but they don't verify against each other — "
            "this should not happen given the writability check above. "
            "config.json.bak still holds the pre-rotation data under the OLD key."
        ) from exc

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
        # Stripped defensively here too, not just by the API route that
        # currently happens to be the only caller — a pasted or manually
        # typed key picking up a trailing newline/space is an easy way to
        # turn a correct key into a rejected one, and this function
        # shouldn't depend on every future caller remembering to strip.
        new_key = new_key_str.strip().encode()
        Fernet(new_key)  # raises if this isn't a valid 32-byte urlsafe-base64 Fernet key
    except Exception as exc:
        raise ValueError("Not a valid encryption key") from exc

    _check_writable()

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

    # Self-verify, same as rotate_key — a write-consistency check, not a
    # "wrong key" signal, hence RuntimeError rather than DecryptionError.
    try:
        verify_raw = CONFIG_FILE.read_bytes()
        verify_key = _read_key_file()
        Fernet(verify_key).decrypt(verify_raw)
    except InvalidToken as exc:
        raise RuntimeError(
            "The new key was saved but config.json doesn't verify against it — this should not "
            "happen given the writability check above."
        ) from exc

    return recovered
