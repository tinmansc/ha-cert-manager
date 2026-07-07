"""TrueNAS CORE 13.x certificate deployer (REST API v2.0)."""
from __future__ import annotations

import datetime
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, ensure_https, secure_key, strip_scheme


def _make_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def check(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _run(cfg: DeviceConfig, local: LocalCert, log: Logger, deploy: bool) -> DeviceResult:
    host = ensure_https(cfg.host)
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    ctx = _make_ctx()

    def api(endpoint: str, method: str = "GET", data=None, exit_on_error: bool = True):
        url = f"{host}/api/v2.0/{endpoint}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx) as r:
                content = r.read().decode()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code}: {e.read().decode()[:200]}"
            if exit_on_error:
                raise RuntimeError(msg)
            log("warn", f"TrueNAS API warning: {msg}")
            return None

    try:
        log("info", f"TrueNAS: checking current certificate at {host}")
        general = api("system/general")
        log("info", "TrueNAS: API key authenticated — system/general OK")
        cert_info = general.get("ui_certificate")

        current_id = None
        current_content = None
        if isinstance(cert_info, dict):
            current_id = cert_info.get("id")
            current_content = cert_info.get("certificate")
        elif isinstance(cert_info, int):
            current_id = cert_info

        if current_content is None and current_id is not None:
            detail = api(f"certificate/id/{current_id}")
            current_content = detail.get("certificate") if detail else None

        local_content = Path(local.cert_path).read_text().strip()

        hostname = strip_scheme(host)

        if current_content and current_content.strip() == local_content:
            log("info", "TrueNAS: certificate already matches — no update needed")
            fp = probe_tls_fingerprint(hostname)
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT,
                message="Certificate current",
                live_fingerprint=fp,
                local_fingerprint=local.fingerprint,
            )

        if not deploy:
            log("info", "TrueNAS: certificate differs (check-only mode)")
            try:
                live_fp = probe_tls_fingerprint(hostname)
            except Exception:
                live_fp = None
            return DeviceResult(
                status=DeployStatus.NEEDS_DEPLOY,
                message="Certificate differs — deploy required",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        cert_name = f"LetsEncrypt_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log("info", f"TrueNAS: creating certificate '{cert_name}'")

        with secure_key(local.key_path) as key_ba:
            api("certificate", method="POST", data={
                "name": cert_name,
                "create_type": "CERTIFICATE_CREATE_IMPORTED",
                "certificate": local_content,
                "privatekey": key_ba.decode(),
            })

        log("info", "TrueNAS: waiting for certificate to be processed…")
        time.sleep(10)

        certs = api("certificate")
        new_id = next((c["id"] for c in certs if c["name"] == cert_name), None)
        if new_id is None:
            raise RuntimeError(f"Could not find uploaded certificate '{cert_name}'")

        log("info", f"TrueNAS: setting certificate {cert_name} (id={new_id}) as active")
        api("system/general", method="PUT", data={"ui_certificate": new_id})

        log("info", "TrueNAS: restarting web UI to apply new certificate")
        api("system/general/ui_restart", method="POST")

        log("success", f"TrueNAS: certificate deployed — keeping old id={current_id} as backup")

        log("info", "TrueNAS: waiting 20 s for web UI to come back up…")
        time.sleep(20)
        try:
            fp = probe_tls_fingerprint(hostname)
        except Exception:
            fp = None
        return DeviceResult(
            status=DeployStatus.DEPLOYED,
            message=f"Deployed {cert_name}",
            live_fingerprint=fp,
            local_fingerprint=local.fingerprint,
        )

    except Exception as exc:
        log("error", f"TrueNAS: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc))
