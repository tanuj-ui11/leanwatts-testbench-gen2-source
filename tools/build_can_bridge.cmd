@echo off
setlocal
set "COMPILER=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%COMPILER%" (
  echo Microsoft .NET Framework 4 compiler is unavailable.
  exit /b 1
)
if not exist "%~dp0..\vendor_can" mkdir "%~dp0..\vendor_can"
"%COMPILER%" /nologo /target:exe /platform:x86 /optimize+ /r:System.Web.Extensions.dll /out:"%~dp0..\vendor_can\CanReceiveBridge.exe" "%~dp0CanReceiveBridge.cs"
exit /b %ERRORLEVEL%
