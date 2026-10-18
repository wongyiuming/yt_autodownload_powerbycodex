# Synthetic process-identity regressions. These are models, NOT real PID reuse.
$ErrorActionPreference = 'Stop'
. (Join-Path (Get-Location).Path 'native_host/process_cleanup_windows.ps1')
$Temp = Join-Path ([System.IO.Path]::GetTempPath()) ('cleanup-model-' + [guid]::NewGuid())
New-Item -ItemType Directory -Force $Temp | Out-Null
$Epoch = [DateTime]::UtcNow.AddSeconds(-5).Ticks
$script:OpenAttempts = @()
$script:Stopped = @()
$script:Snapshots = @()
$script:Scan = 0
$script:Models = @{}

function New-Model {
    param([int]$Id, [long]$Ticks, [int]$Parent = 0, [string]$Name = 'worker.exe', [string]$Path = '')
    return [pscustomobject]@{ ProcessId = $Id; CreationDate = [DateTime]::new($Ticks, [DateTimeKind]::Utc)
        ParentProcessId = $Parent; Name = $Name; ExecutablePath = $Path }
}
function Open-HostCleanupProcess {
    param($Record)
    $Key = Get-HostProcessKey $Record
    $script:OpenAttempts += $Key
    # Lookup by both PID and creation timestamp, as the real handle wrapper does.
    return $script:Models[$Key]
}
function Get-HostBootTicks { return $Epoch - 10000000 }
function Get-HostProcessSnapshot {
    $Index = [Math]::Min($script:Scan, $script:Snapshots.Count - 1)
    $script:Scan++
    return $script:Snapshots[$Index]
}
function Reset-Model {
    param([string]$Name)
    $script:OpenAttempts = @(); $script:Stopped = @(); $script:Scan = 0; $script:Models = @{}
    $Dir = Join-Path $Temp $Name
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $Marker = Join-Path $Dir 'upgrade-in-progress'
    [System.IO.File]::WriteAllText($Marker, 'model')
    (Get-Item $Marker).LastWriteTimeUtc = [DateTime]::new($Epoch, [DateTimeKind]::Utc)
    return $Dir
}
function Add-ModelHandle {
    param($Process)
    $Record = ConvertTo-HostProcessRecord $Process
    $Key = Get-HostProcessKey $Record
    $Handle = [pscustomobject]@{ ExitTicks = [long]0; Key = $Key }
    $Handle | Add-Member ScriptMethod Stop {
        $script:Stopped += $this.Key
        $this.ExitTicks = $Epoch + 30000000
    }
    $Handle | Add-Member ScriptMethod Dispose {}
    $script:Models[$Key] = $Handle
}
try {
    # Root dies, PID is reused by an unrelated parent, then that new parent has
    # a child. Child creation after the original root's exact exit excludes it.
    $Dir = Reset-Model 'recycled-parent'
    $Root = New-Model 100 ($Epoch + 1000000) 1 'yt_autodownload_host.exe' (Join-Path $Dir 'yt_autodownload_host.exe')
    $Replacement = New-Model 100 ($Epoch + 40000000) 1
    $Innocent = New-Model 101 ($Epoch + 50000000) 100
    Add-ModelHandle $Root
    Add-ModelHandle $Replacement
    Add-ModelHandle $Innocent
    $Baseline = New-Model 1 ($Epoch - 10000000) 0
    $script:Snapshots = @{ 0 = @($Baseline, $Root); 1 = @($Baseline, $Replacement, $Innocent) }
    Stop-InstalledHostTrees -InstallDir $Dir
    if ($script:Stopped.Count -ne 1 -or $script:Stopped[0] -ne (Get-HostProcessKey (ConvertTo-HostProcessRecord $Root))) {
        throw 'Recycled parent PID caused an unrelated termination.'
    }
    Write-Host 'MODEL_RECYCLED_PARENT_LIFETIME_OK'

    # Captured PID identity is stale; neither its new occupant nor its child may
    # be terminated. Unknown ancestry must preserve the journal and the gate.
    $Dir = Reset-Model 'stale-parent'
    $Stale = @{ ProcessId = 100; CreationTicks = $Epoch + 1000000 }
    Save-HostCleanupJournal -Path (Join-Path $Dir 'upgrade-processes.json') -Members @($Stale) -StartedTicks $Epoch
    $script:Models = @{}
    Add-ModelHandle $Replacement
    Add-ModelHandle $Innocent
    $script:Snapshots = @{ 0 = @($Baseline, $Replacement, $Innocent) }
    $Blocked = $false
    try { Stop-InstalledHostTrees -InstallDir $Dir -TimeoutSeconds 0 } catch {
        if ($_.Exception.Message -notmatch 'upgrade remains blocked') { throw }
        $Blocked = $true
    }
    if (-not $Blocked -or $script:Stopped.Count -or -not (Test-Path (Join-Path $Dir 'upgrade-processes.json'))) {
        throw 'Ambiguous PID reuse did not fail closed.'
    }
    Write-Host 'MODEL_AMBIGUOUS_RECYCLED_PID_BLOCKED_OK'

    # An intermediate exists entirely between snapshots; its surviving child
    # has no observable parent. It must be journaled as uncertain, never ignored.
    $Dir = Reset-Model 'invisible-intermediate'
    $Root = New-Model 200 ($Epoch + 1000000) 1 'yt_autodownload_host.exe' (Join-Path $Dir 'yt_autodownload_host.exe')
    $Orphan = New-Model 202 ($Epoch + 20000000) 201
    Add-ModelHandle $Root
    Add-ModelHandle $Orphan
    $script:Snapshots = @{ 0 = @($Baseline, $Root); 1 = @($Baseline, $Orphan) }
    $Blocked = $false
    try { Stop-InstalledHostTrees -InstallDir $Dir -TimeoutSeconds 1 } catch {
        if ($_.Exception.Message -notmatch 'upgrade remains blocked') { throw }
        $Blocked = $true
    }
    $Journal = Get-Content (Join-Path $Dir 'upgrade-processes.json') -Raw | ConvertFrom-Json
    if (-not $Blocked -or -not ($Journal.Records | Where-Object { $_.ProcessId -eq 202 -and $_.Uncertain }) -or
        $script:Stopped -contains (Get-HostProcessKey (ConvertTo-HostProcessRecord $Orphan))) {
        throw 'Unobserved intermediate escaped the barrier or caused a guessed kill.'
    }
    Write-Host 'MODEL_UNOBSERVED_INTERMEDIATE_BLOCKED_OK'

    # A provider/precision mismatch must never turn a visible live Host into
    # "already exited" merely because the handle identity check returned null.
    $UnverifiedDir = Reset-Model 'unverified-root'
    $Root = New-Model 300 ($Epoch + 1000000) 1 'yt_autodownload_host.exe' (Join-Path $UnverifiedDir 'yt_autodownload_host.exe')
    $script:Snapshots = @{ 0 = @($Baseline, $Root) }
    $Blocked = $false
    try { Stop-InstalledHostTrees -InstallDir $UnverifiedDir -TimeoutSeconds 0 } catch {
        if ($_.Exception.Message -notmatch 'upgrade remains blocked') { throw }
        $Blocked = $true
    }
    if (-not $Blocked -or -not (Test-Path (Join-Path $UnverifiedDir 'upgrade-processes.json'))) {
        throw 'Unverified live identity was mistaken for process exit.'
    }
    Write-Host 'MODEL_UNVERIFIED_LIVE_IDENTITY_BLOCKED_OK'

    # Reboot recovery discards pre-boot identities and resets the orphan horizon.
    & {
        function Get-HostBootTicks { return $Epoch + 60000000 }
        $script:Snapshots = @{ 0 = @() }
        Stop-InstalledHostTrees -InstallDir $Dir
    }
    if (Test-Path (Join-Path $Dir 'upgrade-processes.json')) { throw 'Reboot recovery retained pre-boot journal.' }
    Write-Host 'MODEL_REBOOT_RECOVERY_OK'
    Write-Host "WINDOWS_CLEANUP_MODEL_OK PS=$($PSVersionTable.PSVersion)"
} finally { Remove-Item -LiteralPath $Temp -Recurse -Force }
