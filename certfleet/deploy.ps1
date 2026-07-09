# deploy.ps1 — Build and deploy CertFleet to Home Assistant
# Usage: .\deploy.ps1 [-nobuild]

param([switch]$nobuild)

$ErrorActionPreference = "Stop"

$ProjectDir = "H:\Projects\HomeAssistant\OC200UploadApp\certfleet"
$SshKey     = "C:\Users\davec\.ssh\PrivSSHKey.ppk"
$SshUser    = "hassio"
$SshHost    = "homeassistant.daveclark.email"
$AddonSlug  = "certfleet"
$TempStage  = "$env:TEMP\certfleet"
$TempTar    = "$env:TEMP\certfleet.tar.gz"

if ($nobuild) {
    Write-Host "[1/4] Skipping build (--nobuild)" -ForegroundColor DarkGray
} else {
    Write-Host "[1/4] Building frontend..." -ForegroundColor Cyan
    Push-Location "$ProjectDir\frontend"
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
    } finally {
        Pop-Location
    }
}

Write-Host "[2/4] Staging files (no node_modules)..." -ForegroundColor Cyan
if (Test-Path $TempStage) { Remove-Item $TempStage -Recurse -Force }
robocopy "$ProjectDir" "$TempStage" /E /XD node_modules __pycache__ .git /XF "*.pyc" "*.pyo" /NFL /NDL /NJH /NJS | Out-Null

Write-Host "[3/4] Archiving..." -ForegroundColor Cyan
if (Test-Path $TempTar) { Remove-Item $TempTar }
tar -czf $TempTar -C $env:TEMP "certfleet"
$sizeMB = [math]::Round((Get-Item $TempTar).Length / 1MB, 2)
Write-Host "    $TempTar ($sizeMB MB)"

Write-Host "[4/4] Uploading and deploying to HA..." -ForegroundColor Cyan
pscp -i $SshKey $TempTar "${SshUser}@${SshHost}:/tmp/certfleet.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "Upload failed" }

$remoteCmd = "cd /tmp && tar xzf certfleet.tar.gz && mkdir -p /addons/$AddonSlug && cp -r certfleet/. /addons/$AddonSlug/ && rm -rf certfleet certfleet.tar.gz && echo 'Files deployed.'"
plink -batch -i $SshKey "${SshUser}@${SshHost}" $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed" }

Write-Host "Done! In HA: Settings -> Apps -> CertFleet -> three-dot menu (⋮) -> Rebuild" -ForegroundColor Green
Write-Host "      (Restart keeps the old Docker image. Rebuild picks up file changes.)" -ForegroundColor DarkGray
Write-Host "      First deploy under the new slug: also uninstall the old 'ha_cert_manager'" -ForegroundColor DarkGray
Write-Host "      add-on in HA once you've confirmed CertFleet started with your devices intact." -ForegroundColor DarkGray
