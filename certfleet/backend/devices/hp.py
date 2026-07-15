"""HP printer certificate deployer (EWS LEDM — PKCS#12 import).

Verified against a real HP DeskJet 2700-series (2752e) EWS, firmware built
2025-12-12. HP's consumer EWS gates its certificate API behind the admin
password (HTTP Basic, realm "admin"): with no password set the cert endpoints
are effectively invisible; once a password is enabled they require auth.

The device serves HTTPS on :443 with a single replaceable device certificate.
Deployment is a PKCS#12 import — same shape as the Brother deployer — to:

  POST /Security/DeviceCertificates/NewCertWithPassword/Upload?fixed_response=true
    multipart/form-data:
      certificate      = the .p12 bundle (leaf + chain + private key)
      password         = the PKCS#12 export password
      pkey_exportable  = "yes"
    -> <err:ErrorInfo><err:HttpCode>201</err:HttpCode><err:Type>created</...>

The uploaded cert becomes the active HTTPS certificate IMMEDIATELY — no
separate "select/activate" step (unlike Brother) and no reboot. So a plain
TLS serial probe before/after is enough to check state and verify a deploy.

Current cert can be read (authenticated) at:
  GET /Security/DeviceCertificates/1/Info  -> <cert:SerialNumber>…</…>

Field names (certificate/password/pkey_exportable) were taken from the EWS's
own CertificateHlp.js import form and confirmed by a real round-trip upload.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from cert_reader import LocalCert, probe_tls_fingerprint, probe_tls_serial
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, ensure_https, secure_key, strip_scheme

REQUEST_TIMEOUT = 30
UPLOAD_PATH = "/Security/DeviceCertificates/NewCertWithPassword/Upload?fixed_response=true"
INFO_PATH = "/Security/DeviceCertificates/1/Info"


def _load_fullchain(path: str) -> list[x509.Certificate]:
    data = Path(path).read_bytes()
    blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", data, flags=re.S
    )
    return [x509.load_pem_x509_certificate(b) for b in blocks]


def _build_pkcs12(cert_path: str, key_ba: bytearray, password: str) -> bytes:
    """Bundle leaf + chain + key into a password-protected PKCS#12.
    key_ba is a mutable bytearray wiped by the caller's secure_key context."""
    certs = _load_fullchain(cert_path)
    leaf, chain = certs[0], certs[1:]
    key = serialization.load_pem_private_key(bytes(key_ba), password=None)
    cn_attrs = leaf.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    name = (cn_attrs[0].value if cn_attrs else "cert").encode()
    return pkcs12.serialize_key_and_certificates(
        name=name, key=key, cert=leaf, cas=chain,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode()),
    )


def _http_code_from_response(text: str) -> Optional[int]:
    m = re.search(r"<err:HttpCode>\s*(\d+)\s*</err:HttpCode>", text)
    return int(m.group(1)) if m else None


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    base = ensure_https(cfg.host)          # HP EWS always serves HTTPS on 443
    hostname = strip_scheme(base)
    port = cfg.port or 443
    username = cfg.username or "admin"
    auth = (username, cfg.password or "")

    # Probe the live cert independently and early — a TLS handshake needs no
    # credentials, so this is still reported even if auth below fails.
    try:
        live_fp = probe_tls_fingerprint(hostname, port)
    except Exception as e:
        log("warn", f"HP [{cfg.name}]: TLS probe failed ({e})")
        live_fp = None

    session = requests.Session()
    session.verify = cfg.verify_tls   # default False — self-signed until first deploy

    try:
        # Authenticated read: confirms credentials work AND surfaces a clear
        # auth error in check, not only at deploy time.
        log("info", f"HP [{cfg.name}]: authenticating to EWS at {base}")
        r = session.get(urljoin(base, INFO_PATH), auth=auth, timeout=REQUEST_TIMEOUT)
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"Authentication failed (HTTP {r.status_code}) — check the EWS admin "
                f"username/password. HP gates the certificate API behind the admin password."
            )
        r.raise_for_status()
        log("info", f"HP [{cfg.name}]: authenticated — certificate API reachable")

        if local is None:
            if deploy:
                raise RuntimeError("No local certificate available to deploy")
            return DeviceResult(
                status=DeployStatus.NO_LOCAL_CERT,
                message="Connected — credentials OK (no local cert to compare)",
                live_fingerprint=live_fp,
            )

        # The served HTTPS cert IS the device cert (single slot) — compare serials.
        try:
            live_serial = probe_tls_serial(hostname, port)
        except Exception:
            live_serial = None

        if live_serial and live_serial == local.serial:
            log("info", f"HP [{cfg.name}]: certificate already current")
            if not deploy:
                return DeviceResult(
                    status=DeployStatus.ALREADY_CURRENT,
                    message="Certificate current",
                    live_fingerprint=live_fp,
                    local_fingerprint=local.fingerprint,
                )
        elif not deploy:
            log("info", f"HP [{cfg.name}]: certificate differs (check-only mode)")
            return DeviceResult(
                status=DeployStatus.NEEDS_DEPLOY,
                message="Certificate differs — deploy required",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        if not deploy:  # already-current, check-only — handled above, but guard anyway
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT, message="Certificate current",
                live_fingerprint=live_fp, local_fingerprint=local.fingerprint,
            )

        # ── deploy: build PKCS#12 and import ─────────────────────────────────
        p12_password = cfg.p12_password or "changeme"
        with secure_key(local.key_path) as key_ba:
            p12 = _build_pkcs12(local.cert_path, key_ba, p12_password)

        log("info", f"HP [{cfg.name}]: importing PKCS#12 certificate")
        # Field order matters: HP's embedded multipart parser rejects (LEDM 400)
        # the request unless the `certificate` file part comes FIRST, before the
        # password/pkey_exportable fields. requests' files=+data= puts data
        # first, so build an ordered list of parts with the file leading.
        # (Confirmed against a real 2752e: file-last -> 400, file-first -> 201.)
        r = session.post(
            urljoin(base, UPLOAD_PATH), auth=auth, timeout=REQUEST_TIMEOUT,
            files=[
                ("certificate", ("certfleet.p12", p12, "application/x-pkcs12")),
                ("password", (None, p12_password)),
                ("pkey_exportable", (None, "yes")),
            ],
        )
        r.raise_for_status()
        code = _http_code_from_response(r.text)
        if code is None or code >= 300:
            raise RuntimeError(f"Certificate import rejected (LEDM HttpCode={code}): {r.text[:200]}")
        log("info", f"HP [{cfg.name}]: import accepted (HttpCode {code}) — applying")

        # Cert applies immediately; give the HTTPS stack a moment, then verify.
        new_serial = None
        for _ in range(6):
            time.sleep(3)
            try:
                new_serial = probe_tls_serial(hostname, port)
            except Exception:
                new_serial = None
            if new_serial == local.serial:
                break
        if new_serial != local.serial:
            raise RuntimeError(
                f"HTTPS verification failed after import: live={new_serial}, expected={local.serial}"
            )

        try:
            new_fp = probe_tls_fingerprint(hostname, port)
        except Exception:
            new_fp = live_fp
        log("success", f"HP [{cfg.name}]: certificate deployed and verified")
        return DeviceResult(
            status=DeployStatus.DEPLOYED,
            message="Deployed and verified",
            live_fingerprint=new_fp,
            local_fingerprint=local.fingerprint,
        )

    except requests.exceptions.RequestException as exc:
        log("error", f"HP [{cfg.name}]: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)
    except Exception as exc:
        log("error", f"HP [{cfg.name}]: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)
