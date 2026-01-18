@echo off
setlocal enabledelayedexpansion

if not exist ".venv\Scripts\python.exe" (
  echo [*] Creating virtualenv...
  py -3 -m venv .venv
)

echo [*] Installing client requirements...
".venv\Scripts\python.exe" -m pip install -r requirements_client.txt

echo [*] Starting Flask chat client on http://127.0.0.1:5050
echo [*] It will talk to the API configured in config.conf
echo.

".venv\Scripts\python.exe" client.py

endlocal

