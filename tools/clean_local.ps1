[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param([switch]$Apply, [switch]$LocalArtifactsReviewed)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'This helper only cleans a local Windows project, never a Linux deployment.'
}

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$rootPrefix = $projectRoot + '\'
if ($projectRoot -notmatch '^[A-Za-z]:\\.+$' -or
    [IO.DriveInfo]::new([IO.Path]::GetPathRoot($projectRoot)).DriveType -ne [IO.DriveType]::Fixed) {
    throw 'The project must be a local fixed-drive directory, not a drive root or network path.'
}

function Get-OptionalItem([string]$Path) {
    try {
        return Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $null
    }
}

function Assert-UnlinkedPath([string]$Path) {
    $ancestor = [IO.Path]::GetFullPath($Path)
    while ($ancestor) {
        $entry = Get-OptionalItem $ancestor
        if ($null -ne $entry -and ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing a linked project path: $ancestor"
        }
        $ancestor = [IO.Path]::GetDirectoryName($ancestor)
    }
}

function Assert-SafePath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not $absolute.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $absolute.Substring(2).Contains(':')) {
        throw "Target is not an ordinary path inside the project: $absolute"
    }
    Assert-UnlinkedPath ([IO.Path]::GetDirectoryName($absolute))
    return $absolute
}

Assert-UnlinkedPath $PSCommandPath
$marker = Join-Path $projectRoot '.project-id'
Assert-UnlinkedPath $marker
$markerEntry = Get-OptionalItem $marker
if ($null -eq $markerEntry -or $markerEntry -isnot [IO.FileInfo] -or
    (Get-Content -LiteralPath $marker -Raw).Trim() -ne 'any-signin-assistant-managed-v1') {
    throw 'Project marker is missing or does not match. Nothing was deleted.'
}

function Get-TreeInfo([string]$Path) {
    $pending = New-Object 'System.Collections.Generic.Stack[System.IO.FileSystemInfo]'
    $links = New-Object 'System.Collections.Generic.List[System.IO.FileSystemInfo]'
    $pending.Push((Get-Item -LiteralPath $Path -Force))
    [long]$bytes = 0
    [long]$files = 0
    while ($pending.Count -gt 0) {
        $entry = $pending.Pop()
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            $links.Add($entry)
        } elseif ($entry -is [IO.DirectoryInfo]) {
            Assert-UnlinkedPath $entry.FullName
            foreach ($child in Get-ChildItem -LiteralPath $entry.FullName -Force) {
                $pending.Push($child)
            }
        } else {
            $bytes += $entry.Length
            $files++
        }
    }
    return [pscustomobject]@{ Bytes = $bytes; Files = $files; Links = $links }
}

$relativeDirectories = @(
    '.local', '.venv', '.release', '.pytest_cache', '.ruff_cache',
    'frontend\node_modules', 'frontend\dist',
    'app\__pycache__', 'app\vendor\__pycache__',
    'scripts\__pycache__', 'tools\__pycache__', 'tools\dev\__pycache__'
)
$relativeFiles = @(
    'frontend\tsconfig.tsbuildinfo', 'frontend\tsconfig.node.tsbuildinfo',
    'tools\verification-backend.json', 'tools\verification-proxies.txt'
)
$relativeTargets = $relativeDirectories + $relativeFiles
$targetPaths = @($relativeTargets | ForEach-Object { Assert-SafePath (Join-Path $projectRoot $_) })
$directoryTargets = @($relativeDirectories | ForEach-Object { Join-Path $projectRoot $_ })

function Assert-TargetType([string]$Path, [IO.FileSystemInfo]$Entry) {
    if (-not ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
        (($directoryTargets -contains $Path) -ne ($Entry -is [IO.DirectoryInfo]))) {
        throw "Cleanup target changed file/directory type: $Path"
    }
}

$plans = @(
    foreach ($target in $targetPaths) {
        $entry = Get-OptionalItem $target
        if ($null -ne $entry) {
            Assert-TargetType $target $entry
            $information = Get-TreeInfo $target
            [pscustomobject]@{ Path = $target; Bytes = $information.Bytes; Files = $information.Files; Links = $information.Links }
        }
    }
)

function Get-ProtectedHashes {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($projectRoot)
    $hashes = @{}
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        Assert-UnlinkedPath $directory
        foreach ($entry in Get-ChildItem -LiteralPath $directory -Force) {
            if ($targetPaths -contains $entry.FullName) {
                continue
            }
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Unexpected link in protected project files: $($entry.FullName)"
            }
            if ($entry -is [IO.DirectoryInfo]) {
                $pending.Push($entry.FullName)
            } else {
                $hashes[$entry.FullName] = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash
            }
        }
    }
    return $hashes
}

function Remove-SafeTree([string]$Path) {
    $absolute = Assert-SafePath $Path
    $entry = Get-Item -LiteralPath $absolute -Force -ErrorAction Stop
    if ($entry -is [IO.DirectoryInfo]) {
        if (-not ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            foreach ($child in Get-ChildItem -LiteralPath $absolute -Force) {
                Remove-SafeTree $child.FullName
            }
            $absolute = Assert-SafePath $absolute
        }
        [IO.Directory]::Delete($absolute, $false)
    } else {
        [IO.File]::Delete($absolute)
    }
}

Write-Output "Project: $projectRoot"
$plans | Select-Object Path, Files, @{Name = 'Links'; Expression = { $_.Links.Count } }, @{Name = 'LogicalMiB'; Expression = { [math]::Round($_.Bytes / 1MB, 2) }} | Format-Table -AutoSize
Write-Output 'Preserves source, lockfiles, .git, and root-level .env, data, secrets, backups and updates.'
Write-Output '.local can contain real verification snapshots, databases, keys and active resume/checkpoint files; it is not all synthetic.'
Write-Output 'Only remove .local and .release after their contents are unused and no unique production data, keys or required rollback archive remain.'
if (-not $Apply) {
    Write-Output 'PREVIEW ONLY. Nothing was deleted. After review and stopping local work, use -Apply -LocalArtifactsReviewed; confirmation is still required.'
    return
}
if (-not $LocalArtifactsReviewed -and @($plans | Where-Object {
    $_.Path -in @((Join-Path $projectRoot '.local'), (Join-Path $projectRoot '.release'))
}).Count -gt 0) {
    throw 'Review .local/.release data, keys, snapshots, active work and rollback needs before supplying -LocalArtifactsReviewed. Nothing was deleted.'
}

$processes = @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, ExecutablePath, CommandLine)
$ancestors = New-Object 'System.Collections.Generic.HashSet[uint32]'
$ancestorId = [uint32]$PID
while ($ancestorId -ne 0 -and $ancestors.Add($ancestorId)) {
    $ancestorProcess = $processes | Where-Object { $_.ProcessId -eq $ancestorId } | Select-Object -First 1
    if ($null -eq $ancestorProcess) {
        break
    }
    $ancestorId = [uint32]$ancestorProcess.ParentProcessId
}
$rootPattern = '(?i)' + [regex]::Escape($projectRoot).Replace('\\', '[\\/]') + '(?:[\\/\s"'']|$)'
$busy = @($processes | Where-Object {
    -not $ancestors.Contains([uint32]$_.ProcessId) -and (
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine -match $rootPattern)
    )
})
if ($busy.Count -gt 0) {
    throw 'A process references this project or its dependencies. Stop preview/test/maintenance work and retry. Nothing was deleted.'
}

$protected = Get-ProtectedHashes
if (-not $PSCmdlet.ShouldProcess($projectRoot, 'Permanently delete only the listed reviewed local artifacts; no recycle bin')) {
    return
}
foreach ($plan in $plans) {
    $target = Assert-SafePath $plan.Path
    $entry = Get-OptionalItem $target
    if ($null -ne $entry) {
        Assert-TargetType $target $entry
        Remove-SafeTree $target
        Write-Output "Removed: $target"
    }
}

$remainingHashes = Get-ProtectedHashes
if ($protected.Count -ne $remainingHashes.Count) {
    throw 'The protected file set changed during cleanup. Inspect the project before continuing.'
}
foreach ($path in $protected.Keys) {
    if (-not $remainingHashes.ContainsKey($path) -or $remainingHashes[$path] -ne $protected[$path]) {
        throw "Protected project file is missing or changed: $path"
    }
}
$remaining = Get-TreeInfo $projectRoot
Write-Output "Complete. Protected files verified: $($protected.Count)."
Write-Output "Remaining ordinary-file logical bytes: $($remaining.Bytes); files: $($remaining.Files)."
Write-Output 'Logical sizes exclude link targets and are not a measurement of physically reclaimed disk blocks.'
