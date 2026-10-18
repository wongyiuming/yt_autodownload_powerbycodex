# Deterministic real-process integration tests. No Pester/module dependency.
$ErrorActionPreference = 'Stop'
$Repo = (Get-Location).Path
$Cleanup = Join-Path $Repo 'native_host\process_cleanup_windows.ps1'
. $Cleanup
$Temp = Join-Path $env:RUNNER_TEMP ('cleanup-races-' + [guid]::NewGuid())
New-Item -ItemType Directory -Force $Temp | Out-Null
$HostExe = Join-Path $Temp 'yt_autodownload_host.exe'
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $Compiler /nologo /target:exe "/out:$HostExe" (Join-Path $Repo 'tools\fixtures\upgrade_host.cs')
if ($LASTEXITCODE -ne 0) { throw 'Cannot compile race fixture.' }
$WorkerExe = Join-Path $Temp 'external-worker.exe'
Copy-Item $HostExe $WorkerExe
$FFmpeg = (python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())').Trim()
$Node = (Get-Command node.exe).Source

function Wait-Until {
    param([scriptblock]$Condition, [string]$Message)
    $Limit = (Get-Date).AddSeconds(20)
    while (-not (& $Condition)) {
        if ((Get-Date) -ge $Limit) { throw $Message }
        Start-Sleep -Milliseconds 25
    }
}
function New-Barrier {
    param([string]$Dir)
    New-Item -ItemType Directory -Force $Dir | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Dir 'upgrade-in-progress'), 'race test')
}
function Start-LateTree {
    param([string]$Dir, [bool]$UseNode)
    New-Item -ItemType Directory -Force $Dir | Out-Null
    Copy-Item $HostExe (Join-Path $Dir 'yt_autodownload_host.exe')
    Copy-Item (Join-Path $Repo 'tools\fixtures\late_worker.js') $Dir
    $ChildExe = if ($UseNode) { $Node } else { $WorkerExe }
    $Mode = if ($UseNode) { '--late-node' } else { '--late' }
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = Join-Path $Dir 'yt_autodownload_host.exe'
    $Info.Arguments = '"' + $ChildExe + '" "' + $FFmpeg + '" "' + $Dir + '" ' + $Mode
    $Info.UseShellExecute = $false
    $Process = [System.Diagnostics.Process]::Start($Info)
    Wait-Until { Test-Path (Join-Path $Dir 'root.pid') } 'Root did not start.'
    return $Process
}
function Release-Worker {
    param([string]$Dir, $RootProcess)
    [System.IO.File]::WriteAllText((Join-Path $Dir 'spawn-worker.signal'), '')
    Wait-Until { $RootProcess.HasExited -and (Test-Path (Join-Path $Dir 'worker.pid')) } 'Root did not exit after spawning.'
}
function Release-FFmpeg {
    param([string]$Dir)
    [System.IO.File]::WriteAllText((Join-Path $Dir 'spawn-ffmpeg.signal'), '')
    Wait-Until {
        (Test-Path (Join-Path $Dir 'ffmpeg.pid')) -and
        (Test-Path (Join-Path $Dir 'writing.wav')) -and
        (Get-Item (Join-Path $Dir 'writing.wav')).Length -gt 4096 -and
        -not (Get-Process -Id ([int](Get-Content (Join-Path $Dir 'worker.pid'))) -ErrorAction SilentlyContinue)
    } ('Worker did not leave a writing FFmpeg orphan in ' + $Dir)
}
function Assert-Retired {
    param([string]$Dir)
    foreach ($Name in @('root', 'worker', 'ffmpeg')) {
        $ProcessId = [int](Get-Content (Join-Path $Dir "$Name.pid"))
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { throw "Leaked $Name PID $ProcessId" }
    }
    $Media = Join-Path $Dir 'writing.wav'
    $Size = (Get-Item $Media).Length
    Start-Sleep -Milliseconds 350
    if ((Get-Item $Media).Length -ne $Size) { throw 'Orphan still writing.' }
    $Exclusive = [System.IO.File]::Open($Media, 'Open', 'ReadWrite', 'None')
    $Exclusive.Dispose()
}
$Roots = @()
try {
    # Run this under both pwsh and Windows PowerShell 5.1 in Actions.
    foreach ($UseNode in @($false, $true)) {
        $Dir = Join-Path $Temp ('late-' + $UseNode)
        New-Barrier $Dir
        $RootProcess = Start-LateTree $Dir $UseNode
        $Roots += $RootProcess
        & {
            # Hook the actual durable-commit boundary, AFTER the snapshot/handle
            # acquisition, BEFORE termination. Real children do not exist until
            # these signals; parents then exit before cleanup can terminate them.
            $OriginalSave = ${function:Save-HostCleanupJournal}
            $script:SaveCount = 0
            function Save-HostCleanupJournal {
                param($Path, $Members, $StartedTicks)
                & $OriginalSave -Path $Path -Members $Members -StartedTicks $StartedTicks
                $script:SaveCount++
                if ($script:SaveCount -eq 1) { Release-Worker $Dir $RootProcess }
                if ($script:SaveCount -eq 2) { Release-FFmpeg $Dir }
            }
            Stop-InstalledHostTrees -InstallDir $Dir -TimeoutSeconds 60
        }
        Assert-Retired $Dir
        # Only the installer releases the marker; helper must not touch it.
        if (-not (Test-Path (Join-Path $Dir 'upgrade-in-progress'))) { throw 'Cleanup released barrier itself.' }
        Stop-InstalledHostTrees -InstallDir $Dir
        Write-Host "LATE_DESCENDANTS_EARLY_PARENT_EXIT_OK node=$UseNode"
        Write-Host "REPEATED_CLEANUP_OK node=$UseNode"
    }

    # Kill the installer at the durable boundary, then recover late descendants
    # from its v2 journal with the original session horizon preserved.
    $Dir = Join-Path $Temp 'interrupted'
    New-Barrier $Dir
    $RootProcess = Start-LateTree $Dir $true
    $Roots += $RootProcess
    $Interrupted = $false
    try {
        & {
            $OriginalSave = ${function:Save-HostCleanupJournal}
            function Save-HostCleanupJournal {
                param($Path, $Members, $StartedTicks)
                & $OriginalSave -Path $Path -Members $Members -StartedTicks $StartedTicks
                Release-Worker $Dir $RootProcess
                throw 'Injected installer interruption after journal commit'
            }
            Stop-InstalledHostTrees -InstallDir $Dir
        }
    } catch {
        if ($_.Exception.Message -notmatch 'Injected installer interruption') { throw }
        $Interrupted = $true
    }
    if (-not $Interrupted -or -not (Test-Path (Join-Path $Dir 'upgrade-processes.json'))) { throw 'Lost recovery journal.' }
    # The child was never journaled. Its parent exited after the crash boundary,
    # so the rerun cannot prove ancestry and must fail closed (no guessed kill).
    $Roots = @($Roots | Where-Object { $_ -ne $RootProcess })
    $RootProcess.Dispose() # Do not let the test's handle keep the dead parent reopenable.
    $Blocked = $false
    try { Stop-InstalledHostTrees -InstallDir $Dir -TimeoutSeconds 1 } catch {
        if ($_.Exception.Message -notmatch 'upgrade remains blocked') { throw }
        $Blocked = $true
    }
    $WorkerId = [int](Get-Content (Join-Path $Dir 'worker.pid'))
    if (-not $Blocked -or -not (Get-Process -Id $WorkerId -ErrorAction SilentlyContinue) -or
        -not (Test-Path (Join-Path $Dir 'upgrade-in-progress'))) { throw 'Uncertain recovery bypassed barrier.' }
    # Even another generation born during the interruption must retain the gate.
    Release-FFmpeg $Dir
    $Blocked = $false
    try { Stop-InstalledHostTrees -InstallDir $Dir -TimeoutSeconds 1 } catch {
        if ($_.Exception.Message -notmatch 'upgrade remains blocked') { throw }
        $Blocked = $true
    }
    if (-not $Blocked) { throw 'Uncertain grandchild escaped recovery gate.' }
    Stop-Process -Id ([int](Get-Content (Join-Path $Dir 'ffmpeg.pid'))) -Force
    Stop-InstalledHostTrees -InstallDir $Dir
    Stop-InstalledHostTrees -InstallDir $Dir
    Write-Host 'INTERRUPTED_LATE_DESCENDANT_FAIL_CLOSED_RECOVERY_OK'

    # Validate against a REAL unrelated live process with a stale creation time.
    # This deterministically exercises PID reuse without claiming Windows actually
    # recycled a particular PID during the test.
    $Dir = Join-Path $Temp 'stale-identity'
    New-Barrier $Dir
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $Node
    $Info.Arguments = '-e "setInterval(() => {}, 1000)"'
    $Info.UseShellExecute = $false
    $Unrelated = [System.Diagnostics.Process]::Start($Info)
    $Roots += $Unrelated
    $Actual = Get-CimInstance Win32_Process -Filter "ProcessId = $($Unrelated.Id)"
    $Stale = @{ ProcessId = $Unrelated.Id; CreationTicks = $Actual.CreationDate.ToUniversalTime().Ticks - 10000000 }
    if ($null -ne (Open-HostCleanupProcess $Stale)) { throw 'Stale identity acquired live handle.' }
    Save-HostCleanupJournal -Path (Join-Path $Dir 'upgrade-processes.json') -Members @($Stale)
    Stop-InstalledHostTrees -InstallDir $Dir
    if ($Unrelated.HasExited) { throw 'PID reuse killed an unrelated process.' }
    Write-Host 'STALE_PID_IDENTITY_REAL_PROCESS_OK'
    Write-Host "WINDOWS_CLEANUP_RACES_OK PS=$($PSVersionTable.PSVersion)"
} catch {
    Get-ChildItem $Temp -Recurse -File | Where-Object { $_.Extension -in @('.pid', '.log', '.wav') } |
        ForEach-Object {
            Write-Host ("FIXTURE_DIAGNOSTIC " + $_.FullName + " bytes=" + $_.Length)
            if ($_.Extension -in @('.pid', '.log')) { Get-Content $_.FullName | Select-Object -First 20 | Out-Host }
        }
    throw
} finally {
    # Test teardown only: every ID comes from a fixture we explicitly created.
    foreach ($PidFile in @(Get-ChildItem $Temp -Filter '*.pid' -Recurse)) {
        $FixtureId = [int](Get-Content $PidFile.FullName)
        if (Get-Process -Id $FixtureId -ErrorAction SilentlyContinue) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $FixtureId /T /F | Out-Host
        }
    }
    foreach ($Process in $Roots) {
        if (-not $Process.HasExited) { $Process.Kill() }
        $Process.Dispose()
    }
}
