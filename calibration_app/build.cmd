@echo off
setlocal
cd /d "%~dp0"
set "COMPILER=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%COMPILER%" (
  echo .NET Framework 4.x compiler is required.
  exit /b 1
)
if not exist bin mkdir bin
"%COMPILER%" /nologo /target:winexe /platform:x86 /optimize+ /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Runtime.Serialization.dll /out:bin\ChargerCalibration.exe Main.cs Core.cs
if errorlevel 1 exit /b 1
"%COMPILER%" /nologo /target:exe /platform:x86 /reference:System.Runtime.Serialization.dll /out:bin\CoreTests.exe Core.cs CoreTests.cs
if errorlevel 1 exit /b 1
echo Build complete. Start bin\ChargerCalibration.exe on an approved Windows workstation.
