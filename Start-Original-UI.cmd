@echo off
setlocal
cd /d "%~dp0"
set "PYQTGRAPH_QT_LIB=PySide6"
set "PYTHON=%~dp0runtime\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0LeanWatts-EoL-0.1.0-win64 (1)\LeanWatts EoL\_internal\python\python.exe"
if not exist "%PYTHON%" (
  echo Extract the complete portable ZIP before starting the application.
  echo The runtime folder must remain beside this launcher.
  pause
  exit /b 1
)
"%PYTHON%" "%~dp0launch_calibration.py" --original-ui %*
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" pause
exit /b %APP_EXIT%
