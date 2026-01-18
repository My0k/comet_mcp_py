@echo off
setlocal enabledelayedexpansion

if not exist ".venv\Scripts\python.exe" (
  echo [*] Creating virtualenv...
  py -3 -m venv .venv
)

echo [*] Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo [*] CLI mode. Usage example:
echo     run_comet_auto_cli.bat "hola"
echo.

".venv\Scripts\python.exe" -m comet_auto.cli %*

endlocal

