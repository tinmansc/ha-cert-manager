# Release Checklist

Run through this before every version bump and push to main.

---

## 1. Code changes complete
- [ ] All intended changes committed locally
- [ ] `run.sh` uses `#!/bin/sh` (NOT `#!/usr/bin/with-contenv bashio` — that requires HA base images)
- [ ] No hardcoded IP addresses, passwords, or test credentials left in code

## 2. Version bump
- [ ] `config.yaml` → `version:` incremented (semver: patch for fixes, minor for new devices/features)
- [ ] `CHANGELOG.md` updated with the new version, date, and bullet points for every change
- [ ] Grep for stray hardcoded version strings before pushing: `grep -rn '"1\.0\.' frontend/src backend --include=*.tsx --include=*.py`
  (the UI version badge reads live from the Supervisor via `/api/supervisor/addon-info` — it should never need editing again, but check anyway in case a new hardcoded copy creeps in)

## 3. config.yaml sanity check
- [ ] `image:` field has NO tag suffix (e.g. `ghcr.io/tinmansc/ha-cert-manager` — no `:latest`, no `:{version}`)
- [ ] `url:` points to `https://github.com/tinmansc/ha-cert-manager`
- [ ] `arch:` lists both `aarch64` and `amd64`
- [ ] `ingress: true` and `ingress_port: 8099` are present

## 4. Frontend built (if frontend changed)
- [ ] `cd frontend && npm run build` completes without errors
- [ ] `frontend/dist/` is up to date (CI rebuilds it, but verify locally if you changed React code)

## 5. Push to GitHub
- [ ] `git status` — no unintended files staged
- [ ] `git push origin main`
- [ ] GitHub Actions workflow triggered (check Actions tab)

## 6. Wait for CI
- [ ] Build and Push to GHCR workflow shows green for the new version
- [ ] Confirm `ghcr.io/tinmansc/ha-cert-manager:<version>` appears in GHCR packages
- [ ] GHCR package visibility is still **Public**

## 7. Verify on test HA (10.10.101.91)
- [ ] Supervisor picks up the new version (may take a few minutes to re-scan the repo)
- [ ] "Update available" appears on the add-on page
- [ ] Update completes without error
- [ ] Add-on starts and shows **Running** (green dot)
- [ ] "Open Web UI" loads the dashboard
- [ ] If upgrading from pre-1.0.10: confirm the existing plaintext `config.json` migrated to encrypted
      storage automatically (check the event log for "Migrating plaintext config.json to encrypted
      storage", and confirm existing devices still show up)

## 8. Deploy to production HA
- [ ] Run `deploy.ps1` if backend or frontend changed
- [ ] In HA: Settings → Apps → Cert Manager → ⋮ → **Rebuild** (not Restart)
- [ ] Add-on restarts and shows Running
- [ ] If touching config storage: confirm `/config/ha_cert_manager/master.key` and `config.json` both
      survive the Rebuild (they live under `/config`, which Rebuild does not touch — only `/data` gets
      reset to `config.yaml` defaults)

## 9. If this release touches encryption/config storage
- [ ] New sensitive device fields need NO special handling — the whole `config.json` is encrypted as one
      blob, not per-field, specifically so this class of "forgot to mark it sensitive" bug can't happen
- [ ] Never derive the encryption key from `SUPERVISOR_TOKEN` — it's reissued by the Supervisor per
      install/start and is not guaranteed to survive a backup restore onto a fresh HA instance (see
      CHANGELOG 1.0.10). The key must live in its own file under `/config` so it travels in the same
      backup archive as the data it protects.
- [ ] Any new write path to `config.json` goes through `crypto_store.save_config()` — never `.write_text()`
      directly — to keep atomic-write + backup-before-overwrite guarantees intact

---

> **Common mistakes we've already made:**
> - `image:` field had `:{version}` tag → Supervisor rejects it silently, add-on never appears in store
> - `run.sh` shebang was `#!/usr/bin/with-contenv bashio` → crashes on startup in plain Python base image
> - Pushed two version bumps back-to-back before CI finished the first → update UI shows error while image is still building
> - Forgot to wait for GHCR build before testing update on HA → "image not found" pull error
> - UI version badge was a hardcoded `APP_VERSION` string in `App.tsx` that drifted 3 releases behind → now reads live from `/api/supervisor/addon-info`
> - Nearly derived the config encryption key from `SUPERVISOR_TOKEN` for convenience → would have made every stored device credential permanently unrecoverable after any HA backup restore, since that token isn't guaranteed to survive reinstall
