"""MP-WICAN-PRO OBD2 adapter certificate deployer (MeatPi WiCAN Pro).

Unlike every other CertFleet device, the WiCAN has NO HTTPS server of its own —
its config UI is plain HTTP, so there is no server certificate to deploy TO it.
Its only TLS facility is a client-side "Certificate Manager": named cert *sets*
(a CA cert to trust an MQTT broker, plus optional client cert+key for mutual
TLS) that the WiCAN's outbound MQTT connection references via `mqtt_cert_set`.

So "deploying a cert" here means a TRUST-STORE push: upload the local Let's
Encrypt fullchain as the CA of a named set, so the WiCAN trusts an MQTT broker
that presents that Let's Encrypt certificate. CertFleet re-pushes on renewal.
(This only needs refreshing if you pin the fullchain/leaf — which rotates
~every 90 days — rather than the stable LE root; pin the root and it's a
one-time deploy.)

API — verified against a real WiCAN Pro (2026-07-14), see its /main.js:
  GET    /cert_manager/sets              -> [{"name","has_ca","has_client_cert","has_client_key"}, ...]
  POST   /cert_manager/sets?name=<name>  -> multipart ca_cert|client_cert|client_key
                                            (needs a CA, OR client cert+key); returns {"ok":true}.
                                            Re-POSTing an existing name OVERWRITES it (idempotent).
  DELETE /cert_manager/sets/<name>       -> {"ok":true}

Limitation, by design of the device: GET returns only which files a set holds
(has_ca/…), never the cert content or a fingerprint. So check() can confirm the
CA set EXISTS but cannot verify its content is current — deploy() (an idempotent
overwrite) is what keeps it fresh on renewal. This is surfaced as a warning.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import requests

from cert_reader import LocalCert
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, strip_scheme

REQUEST_TIMEOUT = 20
DEFAULT_SET_NAME = "certfleet"

# The device can't report cert-set content, only presence — so a "present" set
# can't be proven current. Surfaced on both the check and deploy paths.
_UNVERIFIABLE_NOTE = (
    "The WiCAN's API reports only that a CA set exists, not its contents, so its "
    "freshness can't be verified here — CertFleet re-pushes it on renewal to keep "
    "it current."
)


def _base_url(cfg: DeviceConfig) -> str:
    """WiCAN serves its config/cert API over plain HTTP. Honour an explicit
    scheme if the user gave one; otherwise build http://host[:port]."""
    host = cfg.host.strip()
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    hostname = strip_scheme(host)
    port = cfg.port or 80
    return f"http://{hostname}" if port == 80 else f"http://{hostname}:{port}"


def _set_name(cfg: DeviceConfig) -> str:
    return (getattr(cfg, "wican_cert_set", None) or DEFAULT_SET_NAME).strip() or DEFAULT_SET_NAME


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    base = _base_url(cfg)
    set_name = _set_name(cfg)
    sets_url = f"{base}/cert_manager/sets"

    # No TLS fingerprint probe here on purpose: the WiCAN has no HTTPS server,
    # so there is no live certificate to fingerprint (every DeviceResult below
    # therefore leaves live_fingerprint at its None default).
    try:
        log("info", f"WiCAN [{cfg.name}]: reading cert sets at {sets_url}")
        r = requests.get(sets_url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        sets = r.json()
        if not isinstance(sets, list):
            raise RuntimeError(f"unexpected /cert_manager/sets response: {str(sets)[:200]}")
        existing = next((x for x in sets if isinstance(x, dict) and x.get("name") == set_name), None)
    except Exception as exc:
        log("error", f"WiCAN [{cfg.name}]: could not reach cert manager ({exc})")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc))

    if local is None:
        if deploy:
            return DeviceResult(status=DeployStatus.ERROR, message="No local certificate available to deploy")
        log("info", f"WiCAN [{cfg.name}]: connected — cert manager reachable (no local cert to compare)")
        return DeviceResult(
            status=DeployStatus.NO_LOCAL_CERT,
            message="Connected — cert manager reachable (no local cert to compare)",
        )

    if not deploy:
        if existing and existing.get("has_ca"):
            log("info", f"WiCAN [{cfg.name}]: CA set '{set_name}' present (content not verifiable)")
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT,
                message=f"CA set '{set_name}' present",
                local_fingerprint=local.fingerprint,
                warning=_UNVERIFIABLE_NOTE,
            )
        log("info", f"WiCAN [{cfg.name}]: CA set '{set_name}' not present — deploy required")
        return DeviceResult(
            status=DeployStatus.NEEDS_DEPLOY,
            message=f"CA set '{set_name}' not present — deploy required",
            local_fingerprint=local.fingerprint,
        )

    # ── deploy ──────────────────────────────────────────────────────────────
    try:
        fullchain = Path(local.cert_path).read_bytes()
    except Exception as exc:
        log("error", f"WiCAN [{cfg.name}]: could not read local certificate ({exc})")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc))

    verb = "Updating" if existing else "Creating"
    log("info", f"WiCAN [{cfg.name}]: {verb} CA set '{set_name}' (uploading fullchain as CA)")
    try:
        r = requests.post(
            sets_url,
            params={"name": set_name},
            files={"ca_cert": ("ca.pem", fullchain, "application/x-pem-file")},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        ok = False
        try:
            ok = r.json().get("ok") is True
        except Exception:
            ok = False
        if not ok:
            raise RuntimeError(f"unexpected upload response: {r.text[:200]}")
    except Exception as exc:
        log("error", f"WiCAN [{cfg.name}]: cert set upload failed ({exc})")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc))

    # Confirm the set now exists with a CA (presence is all the API exposes).
    try:
        after = requests.get(sets_url, timeout=REQUEST_TIMEOUT).json()
        now = next((x for x in after if isinstance(x, dict) and x.get("name") == set_name), None)
        if not (now and now.get("has_ca")):
            raise RuntimeError("uploaded CA set not present after upload")
    except Exception as exc:
        log("error", f"WiCAN [{cfg.name}]: post-upload verification failed ({exc})")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc))

    log("success", f"WiCAN [{cfg.name}]: CA set '{set_name}' deployed")
    return DeviceResult(
        status=DeployStatus.DEPLOYED,
        message=f"Deployed fullchain as CA of set '{set_name}'",
        local_fingerprint=local.fingerprint,
        warning=(
            f"For this to take effect, point the WiCAN's MQTT config at cert set "
            f"'{set_name}' (mqtt_cert_set), enable TLS security, and use an mqtts:// "
            f"broker URL. {_UNVERIFIABLE_NOTE}"
        ),
    )
