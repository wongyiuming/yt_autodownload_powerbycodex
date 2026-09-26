param(
    [string]$ExtensionId = "kcmjdacpahcjfomfmecnamfbialfnink"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$InstallDir = Join-Path $env:LOCALAPPDATA "YTAutodownload"
$BinDir = Join-Path $InstallDir "bin"
$HostExe = Join-Path $BinDir "yt_autodownload_host.exe"
$ManifestPath = Join-Path $InstallDir "com.wongyiuming.yt_autodownload.json"

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

$Prebuilt = @(
    (Join-Path $Root "native_host\yt_autodownload_host.exe"),
    (Join-Path $Root "dist\yt_autodownload_host.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($Prebuilt) {
    Write-Host "[1/4] Installing prebuilt native host..."
    Copy-Item $Prebuilt $HostExe -Force
} else {
    Write-Host "[1/4] No prebuilt host found. Building locally..."
    $Python = $null
    foreach ($candidate in @("py", "python")) {
        try {
            $cmd = Get-Command $candidate -ErrorAction Stop
            $Python = $cmd.Source
            if ($candidate -eq "py") { $PythonArgs = @("-3") } else { $PythonArgs = @() }
            break
        } catch {}
    }
    if (-not $Python) { throw "Python 3 was not found. Use a GitHub Actions release package or install Python 3 first." }

    $Venv = Join-Path $InstallDir "build-venv"
    & $Python @PythonArgs -m venv $Venv
    $VenvPython = Join-Path $Venv "Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r (Join-Path $Root "native_host\requirements.txt") pyinstaller
    & $VenvPython -m PyInstaller --clean --onefile --name yt_autodownload_host `
        --distpath $BinDir --workpath (Join-Path $InstallDir "build") `
        --specpath (Join-Path $InstallDir "build") `
        --collect-all yt_dlp --collect-all imageio_ffmpeg `
        (Join-Path $Root "native_host\host.py")
}

Write-Host "[2/4] Installing Node.js runtime if bundled/found..."
$BundledNode = Join-Path $Root "runtime\node.exe"
if (Test-Path $BundledNode) {
    Copy-Item $BundledNode (Join-Path $BinDir "node.exe") -Force
} else {
    $Node = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($Node) {
        Copy-Item $Node.Source (Join-Path $BinDir "node.exe") -Force
    } else {
        Write-Warning "Node.js was not found. The downloader can still start, but some YouTube formats/challenges may fail."
    }
}

Write-Host "[3/4] Registering Chrome Native Messaging host..."
$EscapedHost = $HostExe.Replace("\", "\\")
$Manifest = @"
{
  "name": "com.wongyiuming.yt_autodownload",
  "description": "YT AutoDownload Queue native host",
  "path": "$EscapedHost",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$ExtensionId/"
  ]
}
"@
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ManifestPath, $Manifest, $Utf8NoBom)
$RegPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.wongyiuming.yt_autodownload"
New-Item -Path $RegPath -Force | Out-Null
Set-Item -Path $RegPath -Value $ManifestPath

Write-Host "[4/4] Done."
Write-Host ""
Write-Host "Chrome extension directory:"
Write-Host (Join-Path $Root "extension")
Write-Host ""
Write-Host "Open chrome://extensions, enable Developer mode, choose Load unpacked, and select the extension directory above."
Start-Process "chrome.exe" "chrome://extensions" -ErrorAction SilentlyContinue
