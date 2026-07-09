"""HPE Comware switch certificate deployer.

Delegates to deploy_cert_hpe_1950.py via subprocess. All switch configuration
comes from the CertFleet device editor — no manual YAML files required.

Device editor fields used:
  Username         → SSH login username
  Password         → SSH login password
  XTD CLI Password → (api_key field) xtd-cli-mode enable password
  Switch IP (SSH)  → (site_id field) management IP for SSH; defaults to hostname
  PKI domain       → Comware pki-domain name (default: hp-1950)
  SSL policy       → Comware ssl server-policy name (default: hp-1950)
  Startup config   → startup_config_path (default: flash:/startup.cfg)

Script lookup order:
  1. cfg.comware_script_path (if set)
  2. /config/scripts/deploy_cert_hpe_1950.py  (user-placed override)
  3. /app/scripts/deploy_cert_hpe_1950.py      (bundled default)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, strip_scheme


_USER_SCRIPT    = "/config/scripts/deploy_cert_hpe_1950.py"
_BUNDLED_SCRIPT = "/app/scripts/deploy_cert_hpe_1950.py"


def _resolve_script(cfg: DeviceConfig) -> str:
    explicit = getattr(cfg, "comware_script_path", None)
    if explicit:
        return explicit
    if Path(_USER_SCRIPT).exists():
        return _USER_SCRIPT
    return _BUNDLED_SCRIPT


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, mode="check")


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, mode="deploy")


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, mode: str) -> DeviceResult:
    script = _resolve_script(cfg)

    if not Path(script).exists():
        msg = (
            f"Comware script not found at {script}. "
            "Place deploy_cert_hpe_1950.py at /config/scripts/ or set comware_script_path."
        )
        log("error", f"Comware [{cfg.name}]: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg)

    hostname = strip_scheme(cfg.host)
    port     = cfg.port or 443

    # Build a single-switch inventory YAML for this device and pass it via --switches-file.
    # This means the user never needs to edit hpe1950_switches.yaml manually.
    switch_entry = {
        "switches": {
            cfg.name: {
                "host":           hostname,
                "ip":             hostname,
                "pki_domain":     cfg.pki_domain     or "hp-1950",
                "ssl_policy":     cfg.ssl_policy      or "hp-1950",
                "startup_config": cfg.startup_config_path or "flash:/startup.cfg",
            }
        }
    }

    switches_tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, prefix="comware_sw_"
        ) as f:
            yaml.dump(switch_entry, f)
            switches_tmp = f.name

        log("info", f"Comware [{cfg.name}]: using script {script}")
        log("info", f"Comware [{cfg.name}]: host {hostname} (SSH + TLS)")

        # TLS fingerprint probe (uses legacy ciphers for HP 1950)
        try:
            live_fp = probe_tls_fingerprint(hostname, port, legacy=True)
            if local is not None:
                match_str = "cert matches" if live_fp == local.fingerprint else "cert differs"
                log("info", f"Comware [{cfg.name}]: TLS probe — {match_str}")
            else:
                log("info", f"Comware [{cfg.name}]: TLS probe OK (no local certificate to compare)")
        except Exception as e:
            log("warn", f"Comware [{cfg.name}]: TLS probe failed ({e}), proceeding with script")
            live_fp = None

        if local is None and mode == "deploy":
            raise RuntimeError("No local certificate available to deploy")

        # Pass credentials via env vars so the user doesn't need /config/secrets.yaml
        env = os.environ.copy()
        env.update({
            "HPE_SWITCH_USER":     cfg.username or "",
            "HPE_SWITCH_PASSWORD": cfg.password  or "",
            "HPE_XTD_PASSWORD":    cfg.api_key   or "",
        })

        cmd = [sys.executable, script,
               "--switches-file", switches_tmp,
               "--target", cfg.name,
               "--check" if mode == "check" else "--apply"]

        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)

        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                level = "error" if "error" in line.lower() else \
                        "warn"  if "warn"  in line.lower() else "info"
                log(level, f"Comware [{cfg.name}]: {line}")

        if result.returncode != 0:
            msg = f"Script exited {result.returncode}"
            return DeviceResult(status=DeployStatus.ERROR, message=msg)

        try:
            new_fp = probe_tls_fingerprint(hostname, port, legacy=True)
        except Exception:
            new_fp = live_fp

        if mode == "deploy":
            status = DeployStatus.DEPLOYED
        elif local is None:
            status = DeployStatus.NO_LOCAL_CERT
        elif new_fp and new_fp == local.fingerprint:
            status = DeployStatus.ALREADY_CURRENT
        else:
            status = DeployStatus.NEEDS_DEPLOY
        log("success", f"Comware [{cfg.name}]: complete")
        return DeviceResult(
            status=status,
            message=f"{'Deployed' if mode == 'deploy' else 'Verified'} successfully"
                     + ("" if local is not None else " (no local cert to compare)"),
            live_fingerprint=new_fp,
            local_fingerprint=local.fingerprint if local is not None else None,
        )

    except subprocess.TimeoutExpired:
        msg = "Script timed out after 300s"
        log("error", f"Comware [{cfg.name}]: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg)
    except Exception as exc:
        log("error", f"Comware [{cfg.name}]: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc))
    finally:
        if switches_tmp:
            Path(switches_tmp).unlink(missing_ok=True)
