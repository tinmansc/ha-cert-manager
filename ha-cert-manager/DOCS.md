# HA Cert Manager

Unified Let's Encrypt certificate deployment dashboard for your network devices. Deploy your HA-managed cert to TrueNAS, Hubitat, pfSense, TP-Link Omada, HPE switches, and Brother printers — all from one place.

## First-time setup

After installing the add-on, configure these controls before hitting Start:

| Control | Recommended | Why |
|---|---|---|
| **Start on boot** | On | Add-on starts automatically when HA restarts |
| **Watchdog** | On | HA restarts the add-on if it crashes |
| **Auto update** | On | Picks up new device support and bug fixes |
| **Show in sidebar** | **--> On <--** | Adds Cert Manager to the HA sidebar for one-click access |

> **Show in sidebar is off by default.** Make sure to toggle it on — otherwise you won't have a shortcut to the dashboard.

Once all four are set, click **Start**.

## Opening the dashboard

After the add-on starts, click **Open Web UI** at the top of the add-on page, or use the **Cert Manager** entry in your sidebar (if you enabled Show in sidebar above).

## Configuring devices

On first launch the dashboard will be empty. Click **Add Device** and fill in the connection details for each network device you want to deploy certificates to. Supported device types:

- **TrueNAS** — TrueNAS CORE and SCALE via REST API
- **Brother MFC** — Brother MFC-J4335DW (and similar models with web-based cert upload)
- **Hubitat** — Hubitat Elevation C-7 via local API
- **HPE Comware 7** — HPE 1950 series switches via SSH (tested on 1950; likely works on any Comware 7 switch)
- **TP-Link Omada** — OC200 / OC300 controllers
- **pfSense** — pfSense firewalls via REST API

## SSL certificate path

The add-on reads your Let's Encrypt certificates from the HA `/ssl` volume (mounted read-only). The standard HA cert paths are:

```
/ssl/fullchain.pem
/ssl/privkey.pem
```

If you use a custom path (e.g. from the Duck DNS or Let's Encrypt add-ons), match those paths in each device's configuration.

## Encrypted device configuration

Device hostnames, usernames, passwords, and API keys are encrypted at rest under
`/config/ha_cert_manager/`:

- `config.json` — your devices, encrypted
- `master.key` — the encryption key
- `config.json.bak` — the previous save, kept as a one-generation backup

Both `config.json` and `master.key` live under `/config`, so they're included in
Home Assistant's own Backups feature automatically — **this is the only thing
that protects you against losing both files at once** (e.g. an SD card failure).
This app has no other copy of your device credentials anywhere. If you don't
have HA backups configured, set that up — it matters more on Pi/SD-card
hardware than on a proper server with RAID and scheduled backups.

Settings → Encryption Key lets you view, copy, rotate, or restore the key from
inside the dashboard. If the dashboard itself won't start, or you need to
inspect the raw data for any other reason, you don't need the app running at
all — a Fernet-encrypted file (which is what `config.json` is) can be decrypted
with five lines of Python anywhere the `cryptography` package is installed.

**Manual recovery, without the app:**

1. Get both files off the device — via the HA Samba/File Editor add-on, or
   `scp`/SSH directly to `/config/ha_cert_manager/config.json` and `master.key`.
2. Run the standalone recovery script bundled in this repo
   (`ha-cert-manager/scripts/decrypt_config.py`, needs only `pip install cryptography`):
   ```
   python3 decrypt_config.py master.key config.json
   python3 decrypt_config.py master.key config.json --out recovered.json
   ```
3. If it reports the key doesn't match, try `config.json.bak` instead of
   `config.json` — it's the previous save, and can still match a different key
   if something (like a key rotation) didn't fully complete.

If both `config.json` and `master.key` are gone, there is nothing left to
recover — this is why the HA backup point above matters.

## Support

Open an issue at [github.com/tinmansc/ha-cert-manager](https://github.com/tinmansc/ha-cert-manager).
