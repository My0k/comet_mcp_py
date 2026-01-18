@echo off
setlocal enabledelayedexpansion

REM Create venv if missing
if not exist ".venv\Scripts\python.exe" (
  echo [*] Creating virtualenv...
  py -3 -m venv .venv
)

REM Install deps (requires internet/pip config)
echo [*] Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo [*] Starting app...
".venv\Scripts\python.exe" -m comet_auto

endlocal

