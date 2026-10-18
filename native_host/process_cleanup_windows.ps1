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
    return ($null -ne $Current -and $Current.CreationDate -eq $Process.CreationDate)
}

function Stop-InstalledHostTrees {
    param([string]$InstallDir)
    $Tree = Get-InstalledHostTree $InstallDir
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
    Write-Host "HOST_PROCESS_TREE_CLEANUP_OK ($($Tree.Members.Count) processes verified exited)"
}
