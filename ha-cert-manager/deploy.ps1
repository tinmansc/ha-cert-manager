# deploy.ps1 — Build and deploy ha-cert-manager to Home Assistant
# Usage: .\deploy.ps1 [-nobuild]

param([switch]$nobuild)

$ErrorActionPreference = "Stop"

$ProjectDir = "H:\Projects\HomeAssistant\OC200UploadApp\ha-cert-manager"
$SshKey     = "C:\Users\davec\.ssh\PrivSSHKey.ppk"
$SshUser    = "hassio"
$SshHost    = "homeassistant.daveclark.email"
$AddonSlug  = "ha_cert_manager"
$TempStage  = "$env:TEMP\ha-cert-manager"
$TempTar    = "$env:TEMP\ha-cert-manager.tar.gz"

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
tar -czf $TempTar -C $env:TEMP "ha-cert-manager"
$sizeMB = [math]::Round((Get-Item $TempTar).Length / 1MB, 2)
Write-Host "    $TempTar ($sizeMB MB)"

Write-Host "[4/4] Uploading and deploying to HA..." -ForegroundColor Cyan
pscp -i $SshKey $TempTar "${SshUser}@${SshHost}:/tmp/ha-cert-manager.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "Upload failed" }

$remoteCmd = "cd /tmp && tar xzf ha-cert-manager.tar.gz && cp -r ha-cert-manager/. /addons/ha_cert_manager/ && rm -rf ha-cert-manager ha-cert-manager.tar.gz && echo 'Files deployed.'"
plink -batch -i $SshKey "${SshUser}@${SshHost}" $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed" }

Write-Host "Done! In HA: Settings -> Apps -> Cert Manager -> three-dot menu (⋮) -> Rebuild" -ForegroundColor Green
Write-Host "      (Restart keeps the old Docker image. Rebuild picks up file changes.)" -ForegroundColor DarkGray
