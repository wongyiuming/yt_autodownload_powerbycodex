# Loaded by the installer and exercised directly by Windows regression tests.
function Get-InstalledHostTree {
    param([string]$InstallDir)
    # Enumeration errors must abort the upgrade, never silently skip cleanup.
    $Snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $Prefix = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\') + '\'
    $Known = @{}
    $Roots = @()
    foreach ($Process in $Snapshot) {
        if ($Process.Name -ne 'yt_autodownload_host.exe') { continue }
        if (-not $Process.ExecutablePath) {
            throw "Cannot identify Host PID $($Process.ProcessId); refusing unsafe upgrade."
        }
        if ($Process.ExecutablePath.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $Roots += $Process
            $Known[[int]$Process.ProcessId] = $Process
        }
    }
    do {
        $Added = $false
        foreach ($Process in $Snapshot) {
            $ProcessId = [int]$Process.ProcessId
            $ParentId = [int]$Process.ParentProcessId
            if (-not $Known.ContainsKey($ProcessId) -and $Known.ContainsKey($ParentId) -and
                $Process.CreationDate -ge $Known[$ParentId].CreationDate) {
                $Known[$ProcessId] = $Process
                $Added = $true
            }
        }
    } while ($Added)
    return @{ Roots = $Roots; Members = @($Known.Values) }
}

function Test-SameProcess {
    param($Process)
    $Current = Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.ProcessId)" -ErrorAction Stop
    # Do not target a recycled PID from the original snapshot.
    $ExpectedTicks = if ($Process.CreationTicks) { [long]$Process.CreationTicks } else {
        $Process.CreationDate.ToUniversalTime().Ticks
    }
    return ($null -ne $Current -and $Current.CreationDate.ToUniversalTime().Ticks -eq $ExpectedTicks)
}

function Save-HostCleanupJournal {
    param([string]$Path, [array]$Members)
    $Records = @($Members | ForEach-Object {
        $Ticks = if ($_.CreationTicks) { [long]$_.CreationTicks } else { $_.CreationDate.ToUniversalTime().Ticks }
        @{ ProcessId = [int]$_.ProcessId; CreationTicks = $Ticks }
    })
    # Commit the complete identity snapshot BEFORE terminating any parent.
    # A rerun can then find external orphans even after an installer crash.
    $Temp = $Path + '.tmp'
    [System.IO.File]::WriteAllText($Temp, (ConvertTo-Json -InputObject $Records))
    if (Test-Path -LiteralPath $Path) {
        # PowerShell coerces $null to an empty string for this .NET overload;
        # NullString preserves the null backup-path argument on PS 5.1 and 7.
        [System.IO.File]::Replace($Temp, $Path, [System.Management.Automation.Language.NullString]::Value)
    } else {
        [System.IO.File]::Move($Temp, $Path)
    }
}

function Stop-InstalledHostTrees {
    param([string]$InstallDir)
    $Tree = Get-InstalledHostTree $InstallDir
    $Journal = Join-Path $InstallDir 'upgrade-processes.json'
    if (Test-Path -LiteralPath $Journal) {
        # PS 5.1 emits a JSON array as a single pipeline object. Assign first
        # and iterate explicitly so [] and nonempty arrays work in both shells.
        $Previous = Get-Content -LiteralPath $Journal -Raw | ConvertFrom-Json
        foreach ($Record in $Previous) {
            if (-not $Record.ProcessId -or -not $Record.CreationTicks) { throw 'Invalid Host cleanup journal.' }
            $Tree.Members = @($Tree.Members) + @($Record)
        }
    }
    Save-HostCleanupJournal -Path $Journal -Members $Tree.Members
    # taskkill /T includes descendants, including PyInstaller's child and
    # FFmpeg/Node processes whose executable paths lie outside InstallDir.
    foreach ($Process in $Tree.Roots) {
        if (Test-SameProcess $Process) {
            Write-Host "Retiring Host process tree PID $($Process.ProcessId)"
            & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.ProcessId /T /F | Out-Host
        }
    }
    # Keep the pre-kill snapshot: if a root exits on its own before taskkill,
    # /T cannot discover its orphaned descendants. Retire those explicitly.
    foreach ($Process in $Tree.Members) {
        if (Test-SameProcess $Process) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.ProcessId /T /F | Out-Host
        }
    }
    $Deadline = (Get-Date).AddSeconds(15)
    do {
        $Alive = @($Tree.Members | Where-Object { Test-SameProcess $_ })
        if ($Alive.Count -eq 0) { break }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $Deadline)
    if ($Alive.Count -gt 0) {
        throw "Host tree cleanup failed; upgrade remains blocked. Surviving PIDs: $($Alive.ProcessId -join ', ')"
    }
    Remove-Item -LiteralPath $Journal -ErrorAction Stop
    Write-Host "HOST_PROCESS_TREE_CLEANUP_OK ($($Tree.Members.Count) processes verified exited)"
}
