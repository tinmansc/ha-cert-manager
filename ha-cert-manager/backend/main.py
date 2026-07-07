"""HA Cert Manager — FastAPI backend."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cert_reader import read_local_cert
from config import DeviceConfig, load_devices, OPTIONS_FILE
from devices.base import DeployStatus, DeviceResult, Logger
import devices.truenas as truenas
import devices.brother as brother
import devices.hubitat as hubitat
import devices.comware as comware
import devices.omada as omada
import devices.pfsense as pfsense


# ── Event log (ring buffer, SSE) ──────────────────────────────────────────────

MAX_LOG = 200
_log_buffer: deque[dict] = deque(maxlen=MAX_LOG)
_log_subscribers: list[asyncio.Queue] = []
_log_counter = 0


def _emit(level: str, message: str, device_id: str | None = None):
    global _log_counter
    _log_counter += 1
    entry = {
        "id": _log_counter,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "msg": message,
        "device": device_id,
    }
    _log_buffer.appendleft(entry)
    for q in _log_subscribers:
        q.put_nowait(entry)


def _make_logger(device_id: str) -> Logger:
    def log(level: str, message: str):
        _emit(level, message, device_id)
    return log


# ── Device status cache ───────────────────────────────────────────────────────

_device_status: dict[str, dict] = {}   # id -> {status, last_run, last_result}
_running: set[str] = set()
_last_backup: dict[str, str] = {}      # device_id -> absolute path of last backup file


def _status_for(dev: DeviceConfig) -> dict:
    s = _device_status.get(dev.id, {})
    return {
        "id": dev.id,
        "name": dev.name,
        "type": dev.type,
        "enabled": dev.enabled,
        "host": dev.host,
        "running": dev.id in _running,
        "last_run": s.get("last_run"),
        "last_status": s.get("last_status"),
        "last_message": s.get("last_message"),
        "live_fingerprint": s.get("live_fingerprint"),
        "pfsense_allow_upload": getattr(dev, "pfsense_allow_upload", False),
    }


# ── App lifespan ──────────────────────────────────────────────────────────────

def _migrate_legacy_config():
    """Move /data/options.json → OPTIONS_FILE if the new location doesn't exist yet."""
    legacy = Path("/data/options.json")
    if not OPTIONS_FILE.exists() and legacy.exists():
        OPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        OPTIONS_FILE.write_text(legacy.read_text())
        _emit("info", f"Migrated config from {legacy} → {OPTIONS_FILE}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    OPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_config()
    _emit("info", "HA Cert Manager started")
    yield
    _emit("info", "HA Cert Manager shutting down")


app = FastAPI(title="HA Cert Manager", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC = Path("/app/static")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    assets_dir = STATIC / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    if not OPTIONS_FILE.exists():
        return {"devices": []}
    return json.loads(OPTIONS_FILE.read_text())


@app.post("/api/config")
def save_config(body: dict):
    OPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPTIONS_FILE.write_text(json.dumps(body, indent=2))
    _emit("info", "Device configuration saved")
    return {"ok": True}


@app.post("/api/verify-host")
def verify_host(body: dict):
    import socket as _socket
    import time as _time
    from devices.base import strip_scheme
    raw  = (body.get("host") or "").strip()
    port = int(body.get("port") or 443)
    host = strip_scheme(raw)
    if not host:
        return {"ok": False, "message": "No hostname provided", "latency_ms": -1}
    start = _time.time()
    try:
        sock = _socket.create_connection((host, port), timeout=5)
        sock.close()
        ms = int((_time.time() - start) * 1000)
        return {"ok": True, "message": f"Reachable — {ms} ms", "latency_ms": ms}
    except _socket.timeout:
        return {"ok": False, "message": f"Timed out after 5 s ({host}:{port})", "latency_ms": -1}
    except ConnectionRefusedError:
        return {"ok": False, "message": f"Connection refused on port {port}", "latency_ms": -1}
    except Exception as e:
        return {"ok": False, "message": str(e), "latency_ms": -1}


@app.get("/api/cert")
def get_cert():
    from cert_reader import DEFAULT_CERT_PATH, DEFAULT_KEY_PATH  # noqa: PLC0415
    try:
        cfg = json.loads(OPTIONS_FILE.read_text()) if OPTIONS_FILE.exists() else {}
        cert_path = cfg.get("cert_path") or DEFAULT_CERT_PATH
        key_path  = cfg.get("key_path")  or DEFAULT_KEY_PATH
        return asdict(read_local_cert(cert_path, key_path))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, f"Permission denied reading certificate file: {e}")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/supervisor/addon-info")
def get_addon_info():
    """Query the HA Supervisor for update availability."""
    import urllib.request as _urllib
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {"update_available": False, "version_latest": None}
    try:
        req = _urllib.Request(
            "http://supervisor/addons/self/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _urllib.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        addon = data.get("data", {})
        return {
            "update_available": addon.get("update_available", False),
            "version_latest": addon.get("version_latest"),
        }
    except Exception:
        return {"update_available": False, "version_latest": None}


@app.get("/api/devices")
def get_devices():
    devices = load_devices()
    return [_status_for(d) for d in devices]


@app.post("/api/devices/{device_id}/check")
async def check_device(device_id: str):
    return await _run_device(device_id, deploy=False)


@app.post("/api/devices/{device_id}/deploy")
async def deploy_device(device_id: str):
    return await _run_device(device_id, deploy=True)


@app.post("/api/devices/{device_id}/backup")
async def backup_device(device_id: str):
    """Trigger an OC200 config backup and save it to /config/backups/omada/."""
    devices = load_devices()
    dev = next((d for d in devices if d.id == device_id), None)
    if dev is None:
        raise HTTPException(404, f"Device '{device_id}' not found")
    if dev.type != "omada":
        raise HTTPException(400, "Backup is only supported for Omada devices")
    if device_id in _running:
        raise HTTPException(409, f"Device '{device_id}' is already running")

    _running.add(device_id)
    _emit("info", f"Starting config backup for {dev.name}", device_id)
    try:
        log = _make_logger(device_id)
        path: str = await asyncio.get_event_loop().run_in_executor(
            None, omada.backup_config, dev, log
        )
        _last_backup[device_id] = path
        _emit("success", f"{dev.name}: backup saved → {path}", device_id)
        return {"path": path, "filename": Path(path).name}
    except Exception as exc:
        _emit("error", f"{dev.name}: backup failed — {exc}", device_id)
        raise HTTPException(500, str(exc))
    finally:
        _running.discard(device_id)


@app.get("/api/devices/{device_id}/backup/latest")
async def download_latest_backup(device_id: str):
    """Download the most recently saved backup file for this device."""
    path = _last_backup.get(device_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "No backup available — run a backup first")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=Path(path).name,
    )


@app.post("/api/devices/deploy-all")
async def deploy_all():
    enabled = [dev for dev in load_devices() if dev.enabled]
    results = await asyncio.gather(*[_run_device(dev.id, deploy=True) for dev in enabled])
    return dict(zip([dev.id for dev in enabled], results))


@app.post("/api/devices/check-all")
async def check_all():
    enabled = [dev for dev in load_devices() if dev.enabled]
    results = await asyncio.gather(*[_run_device(dev.id, deploy=False) for dev in enabled])
    return dict(zip([dev.id for dev in enabled], results))


@app.get("/api/events")
async def event_stream():
    q: asyncio.Queue = asyncio.Queue()
    _log_subscribers.append(q)

    async def generate() -> AsyncGenerator[str, None]:
        # Send buffered history first
        for entry in reversed(list(_log_buffer)):
            yield f"data: {json.dumps(entry)}\n\n"
        try:
            while True:
                entry = await asyncio.wait_for(q.get(), timeout=30)
                yield f"data: {json.dumps(entry)}\n\n"
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                _log_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/{full_path:path}", response_class=HTMLResponse)
async def serve_spa(full_path: str = ""):
    index = STATIC / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>HA Cert Manager</h1><p>Frontend not built yet.</p>")


# ── Device dispatch ───────────────────────────────────────────────────────────

_DEPLOYERS = {
    "truenas": (truenas.check, truenas.deploy),
    "brother": (brother.check, brother.deploy),
    "hubitat": (hubitat.check, hubitat.deploy),
    "comware": (comware.check, comware.deploy),
    "omada":   (omada.check,   omada.deploy),
    "pfsense": (pfsense.check, pfsense.deploy),
}


async def _run_device(device_id: str, deploy: bool) -> dict:
    devices = load_devices()
    dev = next((d for d in devices if d.id == device_id), None)
    if dev is None:
        raise HTTPException(404, f"Device '{device_id}' not found")

    if device_id in _running:
        raise HTTPException(409, f"Device '{device_id}' is already running")

    deployers = _DEPLOYERS.get(dev.type)
    if deployers is None:
        raise HTTPException(400, f"Unknown device type '{dev.type}'")

    _running.add(device_id)
    action = "deploy" if deploy else "check"
    _emit("info", f"Starting {action} for {dev.name}", device_id)

    try:
        cfg_data = json.loads(OPTIONS_FILE.read_text()) if OPTIONS_FILE.exists() else {}
        from cert_reader import DEFAULT_CERT_PATH, DEFAULT_KEY_PATH
        local = read_local_cert(
            cfg_data.get("cert_path") or DEFAULT_CERT_PATH,
            cfg_data.get("key_path")  or DEFAULT_KEY_PATH,
        )
        log = _make_logger(device_id)
        fn = deployers[1] if deploy else deployers[0]

        result: DeviceResult = await asyncio.get_event_loop().run_in_executor(
            None, fn, dev, local, log
        )

        _device_status[device_id] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_status": result.status.value,
            "last_message": result.message,
            "live_fingerprint": result.live_fingerprint,
        }
        level = "success" if result.status != DeployStatus.ERROR else "error"
        _emit(level, f"{dev.name}: {result.message}", device_id)
        return _status_for(dev)

    except Exception as exc:
        _device_status[device_id] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_status": "error",
            "last_message": str(exc),
            "live_fingerprint": None,
        }
        _emit("error", f"{dev.name}: unexpected error — {exc}", device_id)
        raise HTTPException(500, str(exc))
    finally:
        _running.discard(device_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="warning", access_log=False)
