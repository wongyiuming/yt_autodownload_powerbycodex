$ErrorActionPreference = "Stop"
$Repo = (Get-Location).Path
$Installer = Join-Path $Repo 'native_host\install_windows.ps1'
$AppDir = Join-Path $env:LOCALAPPDATA 'YTAutodownload'
$Marker = Join-Path $AppDir 'upgrade-in-progress'
$ManifestPath = Join-Path $AppDir 'com.wongyiuming.yt_autodownload.json'
$InstalledExe = (Get-Content $ManifestPath -Raw | ConvertFrom-Json).path
$FixtureDir = Join-Path $env:RUNNER_TEMP ('upgrade-tree-' + [guid]::NewGuid())
$LegacyDir = Join-Path $AppDir 'legacy-fixture'
New-Item -ItemType Directory -Force $FixtureDir, $LegacyDir | Out-Null
$LegacyExe = Join-Path $LegacyDir 'yt_autodownload_host.exe'
$WorkerExe = Join-Path $FixtureDir 'external-worker.exe'
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $Compiler /nologo /target:exe "/out:$LegacyExe" (Join-Path $Repo 'tools\fixtures\upgrade_host.cs')
if ($LASTEXITCODE -ne 0) { throw 'Cannot compile legacy process-tree fixture.' }
Copy-Item $LegacyExe $WorkerExe
$FFmpeg = (python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())').Trim()
$Fixture = $null
$BlockedHost = $null
$Pids = @()
try {
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $LegacyExe
    $Info.Arguments = '"' + $WorkerExe + '" "' + $FFmpeg + '" "' + $FixtureDir + '"'
    $Info.UseShellExecute = $false
    $Fixture = [System.Diagnostics.Process]::Start($Info)
    $Deadline = (Get-Date).AddSeconds(20)
    $Media = Join-Path $FixtureDir 'writing.wav'
    do {
        if ((Test-Path (Join-Path $FixtureDir 'ffmpeg.pid')) -and
            (Test-Path $Media) -and (Get-Item $Media).Length -gt 4096) { break }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $Deadline)
    if (-not (Test-Path $Media) -or (Get-Item $Media).Length -le 4096) {
        throw 'Real FFmpeg fixture did not begin writing.'
    }
    foreach ($Name in @('root', 'worker', 'ffmpeg')) {
        $Pids += [int](Get-Content (Join-Path $FixtureDir "$Name.pid"))
    }
    foreach ($ProcessId in $Pids) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { throw "Fixture PID $ProcessId not alive" }
    }
    Write-Host "REAL_FFMPEG_TREE_READY $($Pids -join ',')"

    # Fail enumeration after registration changes. Installation must throw,
    # leave a durable barrier, and must not report successful activation.
    $Failed = $false
    try {
        & {
            function Get-CimInstance { throw 'Injected process enumeration failure' }
            & $Installer -NoLaunch
        }
    } catch {
        if ($_.Exception.Message -notmatch 'Injected process enumeration failure') { throw }
        $Failed = $true
    }
    if (-not $Failed -or -not (Test-Path $Marker)) { throw 'Failed upgrade did not retain barrier.' }
    Write-Host 'UPGRADE_CLEANUP_FAILURE_BLOCKED_OK'

    # Simulate Chrome reconnecting after a failed/interrupted installer. The
    # real frozen Host must not emit hello, restore state, or start its worker.
    $State = Join-Path $AppDir 'state.json'
    $BeforeState = if (Test-Path $State) { [System.IO.File]::ReadAllText($State) } else { $null }
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $InstalledExe
    $Info.UseShellExecute = $false
    $Info.RedirectStandardInput = $true
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    $BlockedHost = [System.Diagnostics.Process]::Start($Info)
    $FirstByte = New-Object byte[] 1
    $Read = $BlockedHost.StandardOutput.BaseStream.ReadAsync($FirstByte, 0, 1)
    Start-Sleep -Seconds 2
    if ($BlockedHost.HasExited -or $Read.IsCompleted) { throw 'New Host bypassed failed-upgrade barrier.' }
    $AfterState = if (Test-Path $State) { [System.IO.File]::ReadAllText($State) } else { $null }
    if ($AfterState -cne $BeforeState) { throw 'Blocked Host modified persisted queue.' }
    Write-Host 'INTERRUPTED_UPGRADE_NEW_HOST_BLOCKED_OK'

    # Simulate interruption after the root was killed but before its children
    # were retired. Recovery must use the durable identity journal, because
    # the external worker/FFmpeg no longer have a live installed Host ancestor.
    . (Join-Path $Repo 'native_host\process_cleanup_windows.ps1')
    $Tree = Get-InstalledHostTree $AppDir
    Save-HostCleanupJournal -Path (Join-Path $AppDir 'upgrade-processes.json') -Members $Tree.Members
    Stop-Process -Id $Fixture.Id -Force
    $Fixture.WaitForExit()
    foreach ($ProcessId in $Pids[1..2]) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { throw 'Orphan fixture exited early.' }
    }
    Write-Host 'INTERRUPTED_CLEANUP_ORPHANS_READY'

    # A successful rerun must kill the legacy root AND the external worker /
    # real FFmpeg grandchild before removing the barrier.
    & $Installer -NoLaunch
    if (Test-Path $Marker) { throw 'Successful upgrade did not release barrier.' }
    foreach ($ProcessId in $Pids) {
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
            throw "Upgrade left process PID $ProcessId alive"
        }
    }
    $Size = (Get-Item $Media).Length
    Start-Sleep -Milliseconds 750
    if ((Get-Item $Media).Length -ne $Size) { throw 'Old FFmpeg still writing output.' }
    $Exclusive = [System.IO.File]::Open($Media, 'Open', 'ReadWrite', 'None')
    $Exclusive.Dispose()
    Write-Host 'REAL_FFMPEG_CHILD_GRANDCHILD_CLEANUP_OK'
    if (Test-Path (Join-Path $AppDir 'upgrade-processes.json')) { throw 'Cleanup journal was not retired.' }
    Write-Host 'INTERRUPTED_CLEANUP_ORPHAN_RECOVERY_OK'
    python tools/smoke_native_host.py $InstalledExe
    if ($LASTEXITCODE -ne 0) { throw 'Post-upgrade Host handshake failed.' }
    Write-Host 'WINDOWS_UPGRADE_REGRESSION_OK'
} finally {
    foreach ($ProcessId in $Pids) {
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $ProcessId /T /F | Out-Host
        }
    }
    foreach ($Process in @($Fixture, $BlockedHost)) {
        if ($null -ne $Process) {
            if (-not $Process.HasExited) {
                & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F | Out-Host
            }
            $Process.Dispose()
        }
    }
}
