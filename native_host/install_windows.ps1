param(
    [string]$ExtensionId = "kcmjdacpahcjfomfmecnamfbialfnink",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$InstallDir = Join-Path $env:LOCALAPPDATA "YTAutodownload"
$VersionsDir = Join-Path $InstallDir "versions"
$ExtensionDir = Join-Path $InstallDir "extension"
$ManifestPath = Join-Path $InstallDir "com.wongyiuming.yt_autodownload.json"
$StatusPath = Join-Path $InstallDir "install-status.json"

New-Item -ItemType Directory -Force -Path $VersionsDir, $ExtensionDir | Out-Null
. (Join-Path $PSScriptRoot "process_cleanup_windows.ps1")
$UpgradeMarker = Join-Path $InstallDir "upgrade-in-progress"
# Serialize installers. The marker survives installer crashes/cleanup failures;
# the file handle only serializes installers and is always released.
$InstallerLock = [System.IO.File]::Open(
    (Join-Path $InstallDir "install.lock"), [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None
)
try {

# Always prefer the prebuilt binary from the official Release ZIP.
$SourceExe = @(
    (Join-Path $Root "native_host\yt_autodownload_host.exe"),
    (Join-Path $Root "dist\yt_autodownload_host.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $SourceExe) {
    Write-Host "[1/5] Source ZIP detected; building isolated host..."
    $Python = $null
    $PythonArgs = @()
    foreach ($candidate in @("py", "python")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            $Python = $command.Source
            if ($candidate -eq "py") { $PythonArgs = @("-3") }
            break
        }
    }
    if (-not $Python) { throw "No Python 3. Download the prebuilt GitHub Release ZIP instead." }

    $Venv = Join-Path $InstallDir "build-venv"
    $BuildDist = Join-Path $InstallDir "build-dist"
    & $Python @PythonArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Python environment." }
    $VenvPython = Join-Path $Venv "Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Failed to update pip." }
    & $VenvPython -m pip install -r (Join-Path $Root "native_host\requirements.txt") pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install build dependencies." }
    & $VenvPython -m PyInstaller --clean --onefile --name yt_autodownload_host --distpath $BuildDist --workpath (Join-Path $InstallDir "build") --specpath (Join-Path $InstallDir "build") --collect-all yt_dlp --collect-all yt_dlp_ejs --collect-all imageio_ffmpeg (Join-Path $Root "native_host\host.py")
    if ($LASTEXITCODE -ne 0) { throw "Failed to build native host." }
    $SourceExe = Join-Path $BuildDist "yt_autodownload_host.exe"
} else {
    Write-Host "[1/5] Using official prebuilt native host..."
}
if (-not (Test-Path $SourceExe)) { throw "Native host EXE is missing: $SourceExe" }

# Each build gets its own versioned directory. An old Chrome connection can
# safely keep its original EXE open while the replacement is installed.
$NodeSource = Join-Path $Root "runtime\node.exe"
if (-not (Test-Path $NodeSource)) {
    $NodeCommand = Get-Command "node.exe" -ErrorAction SilentlyContinue
    $NodeSource = if ($NodeCommand) { $NodeCommand.Source } else { $null }
}
$HostDigest = (Get-FileHash -Path $SourceExe -Algorithm SHA256).Hash.ToLowerInvariant()
$NodeDigest = if ($NodeSource) {
    (Get-FileHash -Path $NodeSource -Algorithm SHA256).Hash.ToLowerInvariant()
} else {
    "none000000000000"
}
$Fingerprint = $HostDigest.Substring(0, 16) + "-" + $NodeDigest.Substring(0, 8)
$VersionDir = Join-Path $VersionsDir $Fingerprint
$InstalledExe = Join-Path $VersionDir "yt_autodownload_host.exe"
New-Item -ItemType Directory -Force -Path $VersionDir | Out-Null

Write-Host "[2/5] Installing binaries without overwriting running processes..."
if (-not (Test-Path $InstalledExe)) {
    Copy-Item -Path $SourceExe -Destination $InstalledExe
} elseif ((Get-FileHash -Path $InstalledExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $HostDigest) {
    throw "Versioned host checksum mismatch; refusing to overwrite a running EXE."
}
if ($NodeSource) {
    $InstalledNode = Join-Path $VersionDir "node.exe"
    if (-not (Test-Path $InstalledNode)) {
        Copy-Item -Path $NodeSource -Destination $InstalledNode
    } elseif ((Get-FileHash -Path $InstalledNode -Algorithm SHA256).Hash.ToLowerInvariant() -ne $NodeDigest) {
        throw "Versioned Node checksum mismatch."
    }
}

Write-Host "[3/5] Verifying the actual installed host..."
$ExtManifestPath = Join-Path $Root "extension\manifest.json"
$ExpectedVersion = (Get-Content -Path $ExtManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version
$DiagnosticsText = & $InstalledExe --diagnostics
if ($LASTEXITCODE -ne 0) { throw "Installed native host failed --diagnostics." }
$Health = $DiagnosticsText | ConvertFrom-Json
if ($Health.host_version -ne $ExpectedVersion) {
    throw "Package mismatch: host=$($Health.host_version), extension=$ExpectedVersion."
}
if (-not $Health.ejs_available) { throw "Installed native host has no EJS solver." }
if (-not $Health.node_compatible) { throw "Node >=22 not found beside the installed host." }
Write-Host "Verified Host $($Health.host_version) | yt-dlp $($Health.yt_dlp_version) | Node $($Health.node_version)"

# Block new workers BEFORE switching registration. A reconnect during cleanup
# may start the new EXE, but it cannot restore or execute persisted tasks yet.
[System.IO.File]::WriteAllText($UpgradeMarker, "Installation in progress; rerun installer if interrupted.")

Write-Host "[4/5] Updating stable extension files..."
Copy-Item -Path (Join-Path $Root "extension\*") -Destination $ExtensionDir -Force -Recurse
$StableVersion = (Get-Content -Path (Join-Path $ExtensionDir "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version
if ($StableVersion -ne $ExpectedVersion) { throw "Extension installation verification failed." }

Write-Host "[5/5] Switching Chrome Native Messaging registration..."
$HostManifest = @{
    name = "com.wongyiuming.yt_autodownload"
    description = "YT AutoDownload Queue native host"
    path = $InstalledExe
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json -Depth 5
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ManifestPath, $HostManifest, $Utf8NoBom)
# Chromium searches the 32-bit registry view before the 64-bit view.
# Update and verify both, so stale legacy entries cannot shadow our new Host.
$RegistrySubKey = "Software\Google\Chrome\NativeMessagingHosts\com.wongyiuming.yt_autodownload"
$RegistryViews = @(
    [Microsoft.Win32.RegistryView]::Registry32,
    [Microsoft.Win32.RegistryView]::Registry64
)
foreach ($RegistryView in $RegistryViews) {
    $Hive = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::CurrentUser, $RegistryView
    )
    try {
        $Entry = $Hive.CreateSubKey($RegistrySubKey)
        if ($null -eq $Entry) { throw "Cannot write Chrome registry: $RegistryView" }
        try {
            $Entry.SetValue("", $ManifestPath, [Microsoft.Win32.RegistryValueKind]::String)
        } finally { $Entry.Dispose() }
    } finally { $Hive.Dispose() }
}
foreach ($RegistryView in $RegistryViews) {
    $Hive = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::CurrentUser, $RegistryView
    )
    try {
        $Entry = $Hive.OpenSubKey($RegistrySubKey)
        if ($null -eq $Entry) { throw "Missing Chrome Host registration in $RegistryView" }
        try {
            $RegisteredManifest = [string]$Entry.GetValue("")
            if (-not [string]::Equals(
                $RegisteredManifest, $ManifestPath, [System.StringComparison]::OrdinalIgnoreCase
            )) {
                throw "Chrome $RegistryView registration mismatch: $RegisteredManifest"
            }
        } finally { $Entry.Dispose() }
    } finally { $Hive.Dispose() }
}
$RegisteredPath = (Get-Content -Path $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).path
if ($RegisteredPath -ne $InstalledExe -or -not (Test-Path $RegisteredPath)) {
    throw "Native Messaging registration verification failed."
}
# Retire all already-running installed Hosts, including same-version reinstalls.
# New instances started after this snapshot are held behind UpgradeMarker.
# If any cleanup or verification fails, leave the marker in place.
Stop-InstalledHostTrees -InstallDir $InstallDir

$InstallStatus = @{
    version = $Health.host_version
    exe = $InstalledExe
    sha256 = $HostDigest
    extension_path = $ExtensionDir
    installed_at = (Get-Date).ToString("o")
} | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($StatusPath, $InstallStatus, $Utf8NoBom)
Remove-Item -LiteralPath $UpgradeMarker -ErrorAction Stop
Write-Host "Old Host trees exited; updated Host may now resume the queue."

Write-Host ""
Write-Host "SUCCESS: Installed Host $($Health.host_version) and extension $StableVersion."
Write-Host "Stable extension directory: $ExtensionDir"
Write-Host "Registered host executable: $InstalledExe"
Write-Host "Diagnostics: & '$InstalledExe' --diagnostics"
Write-Host "IMPORTANT: In chrome://extensions, load/reload the STABLE extension directory above."
Write-Host "Previously installed Host processes were disconnected automatically when found."
if (-not $NoLaunch) {
    $Chrome = Get-Command "chrome.exe" -ErrorAction SilentlyContinue
    if ($Chrome) { Start-Process $Chrome.Source "chrome://extensions" }
}

} finally {
    $InstallerLock.Dispose()
}
