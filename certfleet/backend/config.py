"""Loads device configuration from the encrypted config store (crypto_store.py)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import crypto_store

# Kept as an alias — other modules import OPTIONS_FILE as "the config file path"
# for logging/display purposes. The file on disk is now an encrypted Fernet
# token, not raw JSON; always go through crypto_store.load_config() /
# save_config() to read or write it.
OPTIONS_FILE = crypto_store.CONFIG_FILE


@dataclass
class DeviceConfig:
    id: str
    name: str
    type: str          # truenas | brother | hubitat | comware | omada | pfsense | netdata
    enabled: bool
    host: str
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    site_id: Optional[str] = None
    pki_domain: Optional[str] = None
    ssl_policy: Optional[str] = None
    startup_config_path: Optional[str] = None
    hubitat_cli_path: str = "/config/scripts/hubitat-cli"
    p12_password: Optional[str] = None
    delete_old_certs: bool = True
    verify_tls: bool = True
    pfsense_allow_upload: bool = False
    proxmox_allow_upload: bool = False
    omadac_id: Optional[str] = None   # 32-char hex; auto-discovered if omitted
    # Jail-hosted devices (netdata, and future grafana/influxdb/gitlab types) —
    # `host`/`port` above stay the TLS-probe target (the jail's own dashboard
    # address), these are for reaching the TrueNAS host that owns the jail.
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    ssh_username: Optional[str] = None
    ssh_private_key: Optional[str] = None   # unencrypted PEM, pasted in device editor
    jail_name: Optional[str] = None
    # WiCAN: name of the cert *set* to create/update in the device's cert
    # manager (the LE fullchain is uploaded as that set's CA). Defaults to
    # "certfleet" in devices/wican.py when unset.
    wican_cert_set: Optional[str] = None


# Non-secret fields that are safe to silently trim of stray edge whitespace at
# read time — a defensive backstop for any value that reached config by a route
# other than the device form (import, restore, hand-edited config.json). Secrets
# (password, api_key, p12_password, ssh_private_key) are deliberately excluded:
# the frontend rejects edge-space secrets rather than mutating them, and trimming
# a passphrase behind the user's back could silently change a real credential.
# See RELEASE_CHECKLIST.md §10 "Trailing whitespace on every user-input field".
def _t(v):
    """Trim a string value; pass through None/non-strings unchanged."""
    return v.strip() if isinstance(v, str) else v


def load_devices(logger=None) -> list[DeviceConfig]:
    raw = crypto_store.load_config(logger=logger)
    devices = []
    for d in raw.get("devices", []):
        devices.append(DeviceConfig(
            id=d["id"],
            name=_t(d["name"]),
            type=d["type"],
            enabled=d.get("enabled", True),
            host=_t(d["host"]),
            port=d.get("port"),
            username=_t(d.get("username")),
            password=d.get("password"),
            api_key=d.get("api_key"),
            site_id=_t(d.get("site_id")),
            pki_domain=_t(d.get("pki_domain")),
            ssl_policy=_t(d.get("ssl_policy")),
            startup_config_path=_t(d.get("startup_config_path")),
            hubitat_cli_path=d.get("hubitat_cli_path", "/config/scripts/hubitat-cli"),
            p12_password=d.get("p12_password", "changeme"),
            delete_old_certs=d.get("delete_old_certs", True),
            verify_tls=d.get("verify_tls", True),
            pfsense_allow_upload=d.get("pfsense_allow_upload", False),
            proxmox_allow_upload=d.get("proxmox_allow_upload", False),
            omadac_id=_t(d.get("omadac_id")),
            ssh_host=_t(d.get("ssh_host")),
            ssh_port=d.get("ssh_port", 22),
            ssh_username=_t(d.get("ssh_username")),
            ssh_private_key=_t(d.get("ssh_private_key")),
            jail_name=_t(d.get("jail_name")),
            wican_cert_set=_t(d.get("wican_cert_set")),
        ))
    return devices
