$ErrorActionPreference = "SilentlyContinue"
$RegPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.wongyiuming.yt_autodownload"
Remove-Item -Path $RegPath -Recurse -Force
Remove-Item -Path (Join-Path $env:LOCALAPPDATA "YTAutodownload\com.wongyiuming.yt_autodownload.json") -Force
Write-Host "Native Messaging registration removed. Downloaded media and queue state were kept under your user profile."
