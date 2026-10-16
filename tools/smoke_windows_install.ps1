$ErrorActionPreference = "Stop"
$Installer = Join-Path (Get-Location).Path "native_host\install_windows.ps1"
$ManifestPath = Join-Path $env:LOCALAPPDATA "YTAutodownload\com.wongyiuming.yt_autodownload.json"
$StableExt = Join-Path $env:LOCALAPPDATA "YTAutodownload\extension\manifest.json"
$RegPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.wongyiuming.yt_autodownload"

& $Installer -NoLaunch
$Registration = (Get-Item $RegPath).GetValue("")
if ($Registration -ne $ManifestPath) { throw "Registry does not point at the installed manifest." }
$Manifest = Get-Content $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Manifest.path.Contains("\versions\")) {
    throw "Installed host is not isolated in a versioned directory."
}
if (-not (Test-Path $Manifest.path)) { throw "Registered host executable is missing." }
$Health = (& $Manifest.path --diagnostics | ConvertFrom-Json)
if ($Health.host_version -ne "1.2.3") { throw "Installed host version mismatched." }
$Extension = Get-Content $StableExt -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Extension.version -ne $Health.host_version) { throw "Installed extension mismatch." }

# Lock the old executable with a no-sharing file handle. Reinstalling the
# same release must succeed without trying to overwrite this running binary.
$Lock = [System.IO.File]::Open($Manifest.path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
try {
    & $Installer -NoLaunch
} finally {
    $Lock.Dispose()
}
$Updated = Get-Content $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Updated.path -ne $Manifest.path) { throw "Idempotent reinstall changed host path." }
python tools/smoke_native_host.py $Updated.path
if ($LASTEXITCODE -ne 0) { throw "Native Messaging handshake failed." }
Write-Host "WINDOWS_INSTALL_SMOKE_OK"
