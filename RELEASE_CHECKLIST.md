# HA Cert Manager — Release Checklist

Run through every item in order before shipping a version bump.

---

## 1. Version bump (all three places must match)

- [ ] `ha-cert-manager/config.yaml` — `version:` field (controls HA update badge)
- [ ] `ha-cert-manager/frontend/src/app/App.tsx` — `APP_VERSION` constant (shown in GUI top-right)
- [ ] `ha-cert-manager/CHANGELOG.md` — new version heading with date and changes

## 2. Code changes

- [ ] All backend changes tested locally or via HA log
- [ ] Frontend build passes: `npm run build` (inside `frontend/`)
- [ ] No TypeScript errors in build output

## 3. Changelog

- [ ] New version section added at the top of `CHANGELOG.md`
- [ ] Every user-visible change documented (Added / Fixed / Changed / Security)
- [ ] Date is correct (YYYY-MM-DD)

## 4. Deploy to HA

- [ ] Run `.\deploy.ps1` from `ha-cert-manager/` — uploads and extracts to `/addons/ha_cert_manager/`
- [ ] In HA: Settings → Add-ons → Cert Manager → ⋮ → **Rebuild** (not Restart)
- [ ] Wait for rebuild to complete; confirm add-on shows new version in the Info tab

## 5. Verify new features / fixes

- [ ] Open Cert Manager in HA ingress and confirm GUI top-right shows new version number
- [ ] Manually test each item listed in the new CHANGELOG section
- [ ] Check EVENT LOG panel for unexpected errors after load
- [ ] Run Verify All and confirm all devices reach expected status
- [ ] If Comware/HP switch was changed: confirm deploy runs without script errors

## 6. End-to-end user flow

- [ ] Reload the add-ons page in HA (Settings → Add-ons) — confirm update badge appeared before Rebuild
- [ ] After Rebuild: open the app cold (hard refresh) and confirm it loads cleanly
- [ ] Add a new device, save, verify it appears correctly — confirm config persists across page reload
- [ ] Check HA add-on log (⋮ → Log) for any Python errors or tracebacks

## 7. Housekeeping

- [ ] Stale files in `/config/scripts/` cleaned up if bundled scripts were updated
  - e.g. delete `/config/scripts/deploy_cert_hpe_1950.py` if user had a manual copy that predates bundling
- [ ] Git commit (when repo is set up)

---

## Quick version-bump command reference

```
# 1. Edit the three version strings
# 2. Build frontend
cd ha-cert-manager\frontend && npm run build

# 3. Deploy
cd ha-cert-manager && .\deploy.ps1 -nobuild

# 4. Rebuild in HA UI
```
