# Loaded by the installer and exercised directly by Windows regression tests.
function Get-HostProcessSnapshot {
    # Enumeration errors must abort the upgrade, never silently skip cleanup.
    return @(Get-CimInstance Win32_Process -ErrorAction Stop)
}

function Get-HostBootTicks {
    return (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime().Ticks
}

function ConvertTo-HostProcessRecord {
    param($Process)
    $Ticks = if ($Process.CreationTicks) { [long]$Process.CreationTicks } else {
        if (-not $Process.CreationDate) { throw 'Missing process creation time.' }
        $Process.CreationDate.ToUniversalTime().Ticks
    }
    return @{ ProcessId = [int]$Process.ProcessId; CreationTicks = $Ticks
        ParentProcessId = [int]$Process.ParentProcessId; ExitTicks = [long]$Process.ExitTicks
        Uncertain = [bool]$Process.Uncertain }
}

function Get-HostProcessKey {
    param($Process)
    return "$($Process.ProcessId):$($Process.CreationTicks)"
}

function Get-InstalledHostTree {
    param([string]$InstallDir, [array]$Snapshot = (Get-HostProcessSnapshot))
    $Separator = [System.IO.Path]::DirectorySeparatorChar
    $Prefix = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd($Separator) + $Separator
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

function Open-HostCleanupProcess {
    param($Record)
    if (-not ('HostCleanupProcess' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'process_identity_windows.cs')
    }
    return [HostCleanupProcess]::Open($Record.ProcessId, $Record.CreationTicks)
}

function Save-HostCleanupJournal {
    param([string]$Path, [array]$Members, [long]$StartedTicks = 0)
    $Records = @($Members | ForEach-Object { ConvertTo-HostProcessRecord $_ })
    $Json = if ($StartedTicks) {
        ConvertTo-Json -InputObject @{ Version = 2; StartedTicks = $StartedTicks; Records = $Records } -Depth 6
    } else { ConvertTo-Json -InputObject $Records -Depth 6 } # Legacy journal test helper.
    $Temp = $Path + '.tmp'
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Json)
    $Stream = [System.IO.File]::Open($Temp, 'Create', 'Write', 'None')
    try { $Stream.Write($Bytes, 0, $Bytes.Length); $Stream.Flush($true) } finally { $Stream.Dispose() }
    if (Test-Path -LiteralPath $Path) {
        [System.IO.File]::Replace($Temp, $Path, [System.Management.Automation.Language.NullString]::Value)
    } else {
        [System.IO.File]::Move($Temp, $Path)
    }
}

function Stop-InstalledHostTrees {
    param([string]$InstallDir, [int]$TimeoutSeconds = 15)
    $Journal = Join-Path $InstallDir 'upgrade-processes.json'
    $Marker = Join-Path $InstallDir 'upgrade-in-progress'
    if (-not (Test-Path -LiteralPath $Marker)) { throw 'Host cleanup requires the upgrade barrier.' }
    # Preserve the beginning of an interrupted attempt, even if it failed before
    # the first journal commit. Never reset this horizon on a reinstall.
    $MarkerInfo = Get-Item -LiteralPath $Marker
    # Cover children created between CreateNew and the final marker write/flush.
    $StartedTicks = [Math]::Min($MarkerInfo.CreationTimeUtc.Ticks, $MarkerInfo.LastWriteTimeUtc.Ticks)
    $Known = @{}
    $Handles = @{}
    if (Test-Path -LiteralPath $Journal) {
        $JournalText = Get-Content -LiteralPath $Journal -Raw
        $Previous = $JournalText | ConvertFrom-Json
        if ($JournalText.TrimStart().StartsWith('{')) {
            if (-not $Previous.Version -or $null -eq $Previous.Records) { throw 'Invalid Host cleanup journal.' }
            if ($Previous.Version -ne 2 -or -not $Previous.StartedTicks) { throw 'Invalid Host cleanup journal.' }
            $StartedTicks = [Math]::Min($StartedTicks, [long]$Previous.StartedTicks)
            $Previous = $Previous.Records
        } elseif (-not $JournalText.TrimStart().StartsWith('[')) { throw 'Invalid Host cleanup journal.' }
        foreach ($Record in $Previous) {
            if (-not $Record.ProcessId -or -not $Record.CreationTicks) { throw 'Invalid Host cleanup journal.' }
            $Entry = ConvertTo-HostProcessRecord $Record
            $Known[(Get-HostProcessKey $Entry)] = $Entry
        }
    }
    # A reboot is a provable boundary: no process from the interrupted attempt
    # can still be alive. Do not mistake new system processes for old orphans.
    $BootTicks = Get-HostBootTicks
    if ($StartedTicks -lt $BootTicks) {
        $Known = @{}
        $StartedTicks = [DateTime]::UtcNow.Ticks
    }
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    try {
        do {
            # Refresh death times BEFORE taking a fresh system-wide snapshot.
            # Keep dead ancestors: a child can be born after the previous scan,
            # survive its parent, and only become visible on this scan.
            $AliveBeforeScan = 0
            foreach ($Key in @($Known.Keys)) {
                if (-not $Handles.ContainsKey($Key) -and -not $Known[$Key].ExitTicks) {
                    $Handle = Open-HostCleanupProcess $Known[$Key]
                    if ($null -ne $Handle) { $Handles[$Key] = $Handle }
                }
                if ($Handles.ContainsKey($Key)) {
                    $Known[$Key].ExitTicks = $Handles[$Key].ExitTicks
                    if (-not $Known[$Key].ExitTicks) { $AliveBeforeScan++ }
                }
            }
            $Snapshot = @(Get-HostProcessSnapshot)
            $Tree = Get-InstalledHostTree -InstallDir $InstallDir -Snapshot $Snapshot
            $Added = 0
            # Discover descendants below using pinned parent lifetimes, rather
            # than trusting ParentProcessId alone in a non-atomic CIM snapshot.
            foreach ($Process in $Tree.Roots) {
                $Record = ConvertTo-HostProcessRecord $Process
                $Key = Get-HostProcessKey $Record
                if (-not $Known.ContainsKey($Key)) { $Known[$Key] = $Record; $Added++ }
            }
            # Pin each identified producer before terminating ANY of them.
            # Retained handles also supply exact exit times after spontaneous exit.
            foreach ($Key in @($Known.Keys)) {
                if (-not $Handles.ContainsKey($Key) -and -not $Known[$Key].ExitTicks) {
                    $Handle = Open-HostCleanupProcess $Known[$Key]
                    if ($null -ne $Handle) { $Handles[$Key] = $Handle }
                }
            }
            do {
                $Expanded = $false
                foreach ($Process in $Snapshot) {
                    if (-not $Process.CreationDate -or [int]$Process.ProcessId -eq 0) { continue }
                    $Record = ConvertTo-HostProcessRecord $Process
                    $Key = Get-HostProcessKey $Record
                    if ($Known.ContainsKey($Key)) { continue }
                    $Parents = @($Known.Values | Where-Object {
                        $_.ProcessId -eq $Record.ParentProcessId -and $_.CreationTicks -le $Record.CreationTicks
                    } | Sort-Object CreationTicks -Descending)
                    if ($Parents.Count -eq 0) { continue }
                    $Parent = $Parents[0]
                    $ParentKey = Get-HostProcessKey $Parent
                    if ($Handles.ContainsKey($ParentKey)) { $Parent.ExitTicks = $Handles[$ParentKey].ExitTicks }
                    # A PID may be reused after the original parent dies. A child
                    # born outside its lifetime is never attributed to that parent.
                    if ($Parent.ExitTicks -and $Record.CreationTicks -gt $Parent.ExitTicks) { continue }
                    $Record.Uncertain = $Parent.Uncertain -or (
                        -not $Handles.ContainsKey($ParentKey) -and -not $Parent.ExitTicks)
                    $Known[$Key] = $Record
                    $Handle = Open-HostCleanupProcess $Record
                    if ($null -ne $Handle) { $Handles[$Key] = $Handle }
                    $Expanded = $true
                    $Added++
                }
            } while ($Expanded)

            # A short-lived intermediate can spawn and disappear entirely between
            # scans. A newly born orphan with no provable parent could be its child.
            # Do not kill unrelated processes on suspicion, and do not declare
            # success while that uncertainty remains. Persist it for recovery.
            $ByPid = @{}
            $CurrentKeys = @{}
            foreach ($Process in $Snapshot) {
                $ByPid[[int]$Process.ProcessId] = $Process
                if ($Process.CreationDate -and [int]$Process.ProcessId -ne 0) {
                    $CurrentKeys[(Get-HostProcessKey (ConvertTo-HostProcessRecord $Process))] = $true
                }
            }
            foreach ($Process in $Snapshot) {
                if (-not $Process.CreationDate -or [int]$Process.ProcessId -eq 0) { continue }
                $Record = ConvertTo-HostProcessRecord $Process
                $Key = Get-HostProcessKey $Record
                if ($Known.ContainsKey($Key) -or $Record.CreationTicks -lt $StartedTicks) { continue }
                $Parent = $ByPid[$Record.ParentProcessId]
                if ($null -ne $Parent -and $Parent.CreationDate -and
                    $Parent.CreationDate -le $Process.CreationDate) { continue }
                $Record.Uncertain = $true
                $Known[$Key] = $Record
                $Handle = Open-HostCleanupProcess $Record
                if ($null -ne $Handle) { $Handles[$Key] = $Handle }
                $Added++
            }
            # Commit every newly discovered identity BEFORE any termination. Keep
            # the full history until a scan begun with ALL producers dead is clean.
            Save-HostCleanupJournal -Path $Journal -Members @($Known.Values) -StartedTicks $StartedTicks
            $Alive = @()
            foreach ($Key in @($Handles.Keys)) {
                $Known[$Key].ExitTicks = $Handles[$Key].ExitTicks
                if (-not $Known[$Key].ExitTicks) {
                    $Alive += $Known[$Key]
                    if (-not $Known[$Key].Uncertain) { $Handles[$Key].Stop() }
                }
            }
            # A matching live snapshot with no acquired handle is NOT evidence
            # of exit (for example a timestamp/provider mismatch). Retry or block.
            $Unverified = @($Known.Keys | Where-Object {
                $CurrentKeys.ContainsKey($_) -and -not $Handles.ContainsKey($_) -and -not $Known[$_].ExitTicks
            })
            if ($Alive.Count -eq 0 -and $Unverified.Count -eq 0 -and $AliveBeforeScan -eq 0 -and $Added -eq 0) {
                Remove-Item -LiteralPath $Journal -ErrorAction Stop
                Write-Host "HOST_PROCESS_TREE_CLEANUP_OK ($($Known.Count) identities, final descendant scan verified)"
                return
            }
            if ((Get-Date) -ge $Deadline) {
                throw "Host cleanup incomplete; upgrade remains blocked. Live or uncertain PIDs: $($Alive.ProcessId -join ', '); unverified identities: $($Unverified -join ', '). Rerun installer after they exit; restart Windows if ancestry cannot be established."
            }
            Start-Sleep -Milliseconds 100
        } while ($true)
    } finally {
        foreach ($Handle in $Handles.Values) { $Handle.Dispose() }
    }
}
