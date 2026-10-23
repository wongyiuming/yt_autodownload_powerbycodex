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
if ($Health.host_version -ne "1.3.0") { throw "Installed host version mismatched." }
$Extension = Get-Content $StableExt -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Extension.version -ne $Health.host_version) { throw "Installed extension mismatch." }

# Simulate a persistent legacy Host connected from LocalAppData\bin and
# a stale registry registration in both views before upgrading it.
$LegacyDir = Join-Path $env:LOCALAPPDATA "YTAutodownload\bin"
New-Item -ItemType Directory -Force -Path $LegacyDir | Out-Null
$LegacyExe = Join-Path $LegacyDir "yt_autodownload_host.exe"
Copy-Item -Path $Manifest.path -Destination $LegacyExe -Force
$LegacyManifestPath = Join-Path $env:LOCALAPPDATA "YTAutodownload\legacy-native-host.json"
$LegacyManifest = @{
    name = "com.wongyiuming.yt_autodownload"
    path = $LegacyExe
    type = "stdio"
    allowed_origins = @("chrome-extension://kcmjdacpahcjfomfmecnamfbialfnink/")
} | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($LegacyManifestPath, $LegacyManifest)
$RegistryKeyName = "Software\Google\Chrome\NativeMessagingHosts\com.wongyiuming.yt_autodownload"
foreach ($View in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
    $Base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, $View)
    try {
        $Key = $Base.CreateSubKey($RegistryKeyName)
        try { $Key.SetValue("", $LegacyManifestPath, [Microsoft.Win32.RegistryValueKind]::String) }
        finally { $Key.Dispose() }
    } finally { $Base.Dispose() }
}
$OldProcInfo = New-Object System.Diagnostics.ProcessStartInfo
$OldProcInfo.FileName = $LegacyExe
$OldProcInfo.UseShellExecute = $false
$OldProcInfo.RedirectStandardInput = $true
$OldProcInfo.RedirectStandardOutput = $true
$OldProcInfo.RedirectStandardError = $true
$OldProcess = [System.Diagnostics.Process]::Start($OldProcInfo)
Start-Sleep -Milliseconds 1000
if ($OldProcess.HasExited) { throw "Legacy Host fixture exited unexpectedly." }
try {
    & $Installer -NoLaunch
    if ($LASTEXITCODE -ne 0) { throw "Installer exited with non-zero status." }
    $OldProcess.Refresh()
    if (-not $OldProcess.HasExited) {
        throw "Installer did NOT terminate the stale persistent Host process."
    }
} finally {
    if (-not $OldProcess.HasExited) { $OldProcess.Kill() }
    $OldProcess.Dispose()
}
foreach ($View in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
    $Base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, $View)
    try {
        $Key = $Base.OpenSubKey($RegistryKeyName)
        try {
            if ([string]$Key.GetValue("") -ne $ManifestPath) {
                throw "$View registry still points to the old EXE manifest."
            }
        } finally { $Key.Dispose() }
    } finally { $Base.Dispose() }
}
Write-Host "LEGACY_HOST_MIGRATION_OK"

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
