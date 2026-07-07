"""Loads device configuration from /data/options.json (HA add-on options)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


OPTIONS_FILE = Path(os.environ.get("OPTIONS_FILE", "/config/ha_cert_manager/config.json"))


@dataclass
class DeviceConfig:
    id: str
    name: str
    type: str          # truenas | brother | hubitat | comware | omada | pfsense
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
    omadac_id: Optional[str] = None   # 32-char hex; auto-discovered if omitted


def load_devices() -> list[DeviceConfig]:
    if not OPTIONS_FILE.exists():
        return []
    raw = json.loads(OPTIONS_FILE.read_text())
    devices = []
    for d in raw.get("devices", []):
        devices.append(DeviceConfig(
            id=d["id"],
            name=d["name"],
            type=d["type"],
            enabled=d.get("enabled", True),
            host=d["host"],
            port=d.get("port"),
            username=d.get("username"),
            password=d.get("password"),
            api_key=d.get("api_key"),
            site_id=d.get("site_id"),
            pki_domain=d.get("pki_domain"),
            ssl_policy=d.get("ssl_policy"),
            startup_config_path=d.get("startup_config_path"),
            hubitat_cli_path=d.get("hubitat_cli_path", "/config/scripts/hubitat-cli"),
            p12_password=d.get("p12_password", "changeme"),
            delete_old_certs=d.get("delete_old_certs", True),
            verify_tls=d.get("verify_tls", True),
            pfsense_allow_upload=d.get("pfsense_allow_upload", False),
            omadac_id=d.get("omadac_id"),
        ))
    return devices
