@echo off
setlocal enabledelayedexpansion

REM Default bind (LAN)
set HOST=0.0.0.0
set PORT=8787

REM Allow overriding port from first arg
if not "%~1"=="" (
  set PORT=%~1
)

if not exist ".venv\Scripts\python.exe" (
  echo [*] Creating virtualenv...
  py -3 -m venv .venv
)

echo [*] Installing API requirements...
".venv\Scripts\python.exe" -m pip install -r requirements_api.txt

echo [*] Starting Comet Auto API on %HOST%:%PORT% ...
echo [*] Note: you may need to allow the port in Windows Firewall.
echo.

".venv\Scripts\python.exe" -m comet_auto.api --host %HOST% --port %PORT%

endlocal

