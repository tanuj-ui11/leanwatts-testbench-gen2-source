param(
    [Parameter(Mandatory=$true)][string]$ArchivePath,
    [Parameter(Mandatory=$true)][string]$ReportPath
)
$ErrorActionPreference = 'Stop'
$releaseRoot = Join-Path $env:LOCALAPPDATA 'Programs\CAN-Calibration-Bench-7.2'
$packageRoot = Join-Path $releaseRoot 'CAN-Calibration'
$expectedArchiveHash = 'b99e030c327ee44e365a44288917b91f1b4a9fadd4a2788f31c45c333288bf5f'
if ((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedArchiveHash) {
    throw 'The installation ZIP does not match the verified release.'
}
if (Test-Path -LiteralPath $releaseRoot) {
    throw 'The 7.2 installation folder already exists. Preserving its files and saved data.'
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
$folders = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
$desktopRoot = [Environment]::ExpandEnvironmentVariables($folders.Desktop)
$startMenuRoot = Join-Path ([Environment]::ExpandEnvironmentVariables($folders.'Start Menu')) 'Programs'
$shellObject = New-Object -ComObject WScript.Shell
$shortcuts = @()
foreach ($shortcutRoot in @($desktopRoot, $startMenuRoot)) {
    if ([string]::IsNullOrWhiteSpace($shortcutRoot)) { throw 'Could not find the user shortcut folder.' }
    New-Item -ItemType Directory -Path $shortcutRoot -Force | Out-Null
    $shortcutPath = Join-Path $shortcutRoot 'CAN Calibration Bench 7.2.lnk'
    if (Test-Path -LiteralPath $shortcutPath) { throw "Preserving an existing shortcut: $shortcutPath" }
    $shortcut = $shellObject.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $packageRoot 'runtime\pythonw.exe'
    $shortcut.Arguments = '"' + (Join-Path $packageRoot 'launch_calibration.py') + '"'
    $shortcut.WorkingDirectory = $packageRoot
    $shortcut.Description = 'CAN charger calibration and Maynuo load bench - load diagnostics 7.2'
    $shortcut.Save()
    $shortcuts += $shortcutPath
}
$report = [ordered]@{
    installed_at = (Get-Date).ToString('o')
    package_root = $packageRoot
    archive_sha256 = $expectedArchiveHash
    manifest_files_verified = $verified
    shortcuts = $shortcuts
    instrument_io = $false
}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReportPath -Encoding utf8
$report | ConvertTo-Json -Depth 5
