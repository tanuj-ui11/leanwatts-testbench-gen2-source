param(
    [Parameter(Mandatory=$true)][string]$ArchivePath,
    [Parameter(Mandatory=$true)][string]$ExpectedArchiveHash,
    [Parameter(Mandatory=$true)][string]$ReportPath
)
$ErrorActionPreference = 'Stop'
$releaseRoot = Join-Path $env:LOCALAPPDATA 'Programs\CAN-Calibration-Bench-7.3'
$packageRoot = Join-Path $releaseRoot 'CAN-Calibration'
$previousData = Join-Path $env:LOCALAPPDATA 'Programs\CAN-Calibration-Bench-7.2\CAN-Calibration\calibration_data'
if ((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedArchiveHash.ToLowerInvariant()) {
    throw 'The installation ZIP does not match the verified release.'
}
if (Test-Path -LiteralPath $releaseRoot) {
    throw 'The 7.3 installation folder already exists. Preserving its files and saved data.'
}
New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
Expand-Archive -LiteralPath $ArchivePath -DestinationPath $releaseRoot
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot 'MANIFEST.json') -Raw | ConvertFrom-Json
$verified = 0
foreach ($entry in $manifest.PSObject.Properties) {
    $filePath = Join-Path $packageRoot $entry.Name
    if ((Get-Item -LiteralPath $filePath).Length -ne $entry.Value.bytes -or
        (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256) {
        throw "Installed file validation failed: $($entry.Name)"
    }
    $verified++
}
$copiedData = $false
$destinationData = Join-Path $packageRoot 'calibration_data'
if (Test-Path -LiteralPath $previousData) {
    if (Test-Path -LiteralPath $destinationData) { throw 'Release unexpectedly includes user data.' }
    Copy-Item -LiteralPath $previousData -Destination $destinationData -Recurse
    $copiedData = $true
}
# User confirmed these draft bounds, but has NOT established trim direction.
# Preserve the original 7.2 file; automatic tuning remains explicitly disabled.
$stationFile = Join-Path $destinationData 'station-load-setup.json'
if (Test-Path -LiteralPath $stationFile) {
    $station = Get-Content -LiteralPath $stationFile -Raw | ConvertFrom-Json
    if ($station.automation_settings.commissioned -eq $true) {
        throw 'Existing tuning is approved; preserve it for review instead of replacing it.'
    }
    $station.automation_settings = [pscustomobject]@{
        commissioned = $false
        current_initial = 80.0
        voltage_initial = 80.0
        current_min = 60.0
        current_max = 85.0
        voltage_min = 60.0
        voltage_max = 85.0
        current_direction = 0
        voltage_direction = 0
    }
    $stationJson = $station | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($stationFile, $stationJson, (New-Object System.Text.UTF8Encoding($false)))
}
$folders = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
$desktopRoot = [Environment]::ExpandEnvironmentVariables($folders.Desktop)
$startMenuRoot = Join-Path ([Environment]::ExpandEnvironmentVariables($folders.'Start Menu')) 'Programs'
$shellObject = New-Object -ComObject WScript.Shell
$shortcuts = @()
foreach ($shortcutRoot in @($desktopRoot, $startMenuRoot)) {
    if ([string]::IsNullOrWhiteSpace($shortcutRoot)) { throw 'Could not find the user shortcut folder.' }
    New-Item -ItemType Directory -Path $shortcutRoot -Force | Out-Null
    $shortcutPath = Join-Path $shortcutRoot 'CAN Calibration Bench 7.3.lnk'
    if (Test-Path -LiteralPath $shortcutPath) { throw "Preserving an existing shortcut: $shortcutPath" }
    $shortcut = $shellObject.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $packageRoot 'runtime\pythonw.exe'
    $shortcut.Arguments = '"' + (Join-Path $packageRoot 'launch_calibration.py') + '"'
    $shortcut.WorkingDirectory = $packageRoot
    $shortcut.Description = 'CAN charger calibration and Maynuo load bench - load control 7.3'
    $shortcut.Save()
    $shortcuts += $shortcutPath
}
$report = [ordered]@{
    installed_at = (Get-Date).ToString('o')
    package_root = $packageRoot
    archive_sha256 = $ExpectedArchiveHash
    manifest_files_verified = $verified
    copied_existing_calibration_data = $copiedData
    automatic_tuning_enabled = $false
    draft_duty_range = @(60.0, 85.0)
    draft_starting_duty = 80.0
    direction_verified = $false
    shortcuts = $shortcuts
    instrument_io = $false
}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReportPath -Encoding utf8
$report | ConvertTo-Json -Depth 5
