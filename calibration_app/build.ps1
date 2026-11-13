$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework 4.x compiler is required on the build machine.' }
$output = Join-Path $root 'bin'
New-Item -ItemType Directory -Force -Path $output | Out-Null
& $compiler /nologo /target:winexe /platform:x86 /optimize+ /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Runtime.Serialization.dll "/out:$output\ChargerCalibration.exe" "$root\Main.cs" "$root\Core.cs"
if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
& $compiler /nologo /target:exe /platform:x86 /reference:System.Runtime.Serialization.dll "/out:$output\CoreTests.exe" "$root\Core.cs" "$root\CoreTests.cs"
if ($LASTEXITCODE -ne 0) { throw 'Test build failed.' }
Write-Output "Built $output\ChargerCalibration.exe"
